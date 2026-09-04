"""Orchestration tests for the fire mask and the raster stage. No network, no HDF."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from kiln_ingest import cli, granule, raster, storage_io
from kiln_ingest.cmr import GranuleRef
from kiln_ingest.granule import GranuleReduction
from kiln_ingest.science import (
    CAUSE_VOLCANIC,
    FIRE_UNAVAILABLE_NOTE,
    QC_NOTE,
    Anomaly,
    GranuleField,
    TileMax,
)

TARGET = date(2026, 8, 30)


class StubResponse:
    def __init__(self, payload=None, status_code=200, body=b"granule-bytes", text=""):
        self._payload = payload
        self.status_code = status_code
        self._body = body
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=None):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def feed(short_name: str, count: int, minutes=(1125, 1130)):
    """A CMR feed of ``count`` granules of one product, on known overpasses."""
    return {"feed": {"entry": [
        {
            "producer_granule_id": f"{short_name}.A2026242.{minutes[i]}.061.NRT.hdf",
            "time_start": f"2026-08-30T11:{25 + 5 * i}:00.000Z",
            "links": [{
                "rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                "href": f"https://nrt3.modaps.eosdis.nasa.gov/{short_name}.{minutes[i]}.hdf",
            }],
        }
        for i in range(count)
    ]}}


class StubSession:
    """Answers CMR searches for either product, plus granule downloads."""

    def __init__(self, lst_count=2, fire_count=2, fire_search_fails=False):
        self.lst_count = lst_count
        self.fire_count = fire_count
        self.fire_search_fails = fire_search_fails
        self.downloads: list[str] = []
        self.writes: list[tuple[str, str, dict]] = []

    def get(self, url, **kwargs):
        if "cmr.earthdata.nasa.gov" in url:
            short_name = kwargs["params"]["short_name"]
            if short_name in ("MOD14", "MYD14"):
                if self.fire_search_fails:
                    raise ConnectionError("CMR is down")
                return StubResponse(payload=feed(short_name, self.fire_count))
            return StubResponse(payload=feed(short_name, self.lst_count))
        self.downloads.append(url)
        return StubResponse()

    def request(self, method, url, **kwargs):
        self.writes.append((method, url, kwargs))
        if url.endswith("ingest_runs"):
            return StubResponse(payload=[{"id": 77}])
        return StubResponse(payload=[])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_field(celsius: float, lat: float, lon: float) -> GranuleField:
    return GranuleField(
        celsius=np.array([[celsius]]),
        keep=np.array([[True]]),
        lat=np.array([[lat]]),
        lon=np.array([[lon]]),
    )


@pytest.fixture
def patched_granule(monkeypatch):
    """Replace the HDF readers with in-memory stand-ins, recording their inputs."""
    seen: dict[str, list] = {"exclusions": [], "notes": [], "volcanic_sources": []}

    def fake_reduction(
        path,
        granule_id,
        observed_at,
        fire_exclusion=None,
        qc_note=QC_NOTE,
        volcanic_sources=None,
    ):
        seen["exclusions"].append(fire_exclusion)
        seen["notes"].append(qc_note)
        seen["volcanic_sources"].append(volcanic_sources)
        tiles = {
            (31, -115): TileMax(31, -115, 58.0, 31.4, -115.2, observed_at, granule_id, qc_note)
        }
        return GranuleReduction(tiles=tiles, pixels=fake_field(58.0, 31.4, -115.2))

    def fake_read_fire(path):
        return np.array([12.005]), np.array([34.005])

    monkeypatch.setattr(granule, "granule_reduction", fake_reduction)
    monkeypatch.setattr(granule, "read_fire_granule", fake_read_fire)
    return seen


# --- fire pairing -------------------------------------------------------------------


def test_fire_granules_are_paired_by_overpass_stamp():
    session = StubSession(fire_count=2)
    urls = cli.discover_fire_granules(session, "MOD11_L2", TARGET)
    assert sorted(urls) == ["A2026242.1125", "A2026242.1130"]
    assert urls["A2026242.1125"].url.endswith("MOD14.1125.hdf")


def test_fire_discovery_failing_leaves_the_day_unmasked_rather_than_failing_it():
    session = StubSession(fire_search_fails=True)
    assert cli.discover_fire_granules(session, "MOD11_L2", TARGET) == {}


def test_an_unpaired_overpass_is_noted_not_masked(tmp_path):
    exclusion, note = cli.fire_exclusion_for(
        StubSession(), "token", tmp_path, {}, "MOD11_L2.A2026242.1125.061.NRT.hdf", None
    )
    assert exclusion is None
    assert note.endswith(FIRE_UNAVAILABLE_NOTE)


def test_a_granule_name_without_a_stamp_is_noted_not_masked(tmp_path):
    exclusion, note = cli.fire_exclusion_for(
        StubSession(), "token", tmp_path, {"A2026242.1125": "u"}, "mystery.hdf", None
    )
    assert exclusion is None
    assert note.endswith(FIRE_UNAVAILABLE_NOTE)


def test_an_unreadable_fire_granule_is_noted_not_masked(tmp_path):
    def explode(path):
        raise ValueError("corrupt FP_latitude")

    exclusion, note = cli.fire_exclusion_for(
        StubSession(),
        "token",
        tmp_path,
        {
            "A2026242.1125": GranuleRef(
                granule_id="MOD14.1125.hdf",
                url="https://nrt3.modaps.eosdis.nasa.gov/MOD14.1125.hdf",
                observed_at="2026-08-30T11:25:00Z",
            )
        },
        "MOD11_L2.A2026242.1125.061.NRT.hdf",
        explode,
    )
    assert exclusion is None
    assert note.endswith(FIRE_UNAVAILABLE_NOTE)


def test_a_readable_fire_granule_yields_keys_and_the_plain_note(tmp_path):
    exclusion, note = cli.fire_exclusion_for(
        StubSession(),
        "token",
        tmp_path,
        {
            "A2026242.1125": GranuleRef(
                granule_id="MOD14.1125.hdf",
                url="https://nrt3.modaps.eosdis.nasa.gov/MOD14.1125.hdf",
                observed_at="2026-08-30T11:25:00Z",
            )
        },
        "MOD11_L2.A2026242.1125.061.NRT.hdf",
        lambda path: (np.array([12.005]), np.array([34.005])),
    )
    assert note == QC_NOTE
    assert exclusion is not None and exclusion.size == 9
    # The downloaded fire granule was cleaned up along with the LST granules.
    assert list(tmp_path.iterdir()) == []


# --- run_product with the extras on ------------------------------------------------


def test_the_extras_are_off_unless_asked_for(patched_granule, monkeypatch):
    """The default path is the plain reader: no fire query, no raster."""
    called = {"n": 0}

    def plain(path, granule_id, observed_at):
        called["n"] += 1
        return {}

    monkeypatch.setattr(granule, "granule_maxima", plain)
    session = StubSession()

    cli.run_product(session, "MOD11_L2", TARGET, "token", None, max_granules=None, dry_run=True)

    assert called["n"] == 2
    assert patched_granule["exclusions"] == []


def test_hot_pixels_from_both_products_share_one_pyramid(patched_granule):
    day = cli.DayAccumulator()
    session = StubSession()

    for product in ("MOD11_L2", "MYD11_L2"):
        cli.run_product(
            session,
            product,
            TARGET,
            "token",
            None,
            max_granules=None,
            dry_run=True,
            day=day,
            fire_masking=True,
        )

    # 31.4 N, 115.2 W is one z7 pixel; both satellites painted the same one.
    assert len(day.raster) == 1
    tile = next(iter(day.raster.values()))
    assert int(tile.max()) == 5800

    # The same accumulator carries the 1-degree maxima the all-time stage reads.
    assert set(day.tiles) == {(31, -115)}


def test_anomalies_found_granule_by_granule_reach_the_day(monkeypatch):
    """Volcanic and wildfire rows are found as each granule is reduced.

    Two overpasses see the same vent at different temperatures. The day keeps
    one row holding the hotter, filed under the product -- which is how the
    unique constraint on kiln.anomaly_readings is shaped.
    """
    temperatures = iter([70.0, 90.37])

    def fake_reduction(
        path,
        granule_id,
        observed_at,
        fire_exclusion=None,
        qc_note=QC_NOTE,
        volcanic_sources=None,
    ):
        max_c = next(temperatures)
        tile = TileMax(13, 40, max_c, 13.59, 40.67, observed_at, granule_id, qc_note)
        return GranuleReduction(
            tiles={},
            pixels=fake_field(max_c, 13.59, 40.67),
            anomalies={
                (13, 40, CAUSE_VOLCANIC): Anomaly(
                    tile=tile, cause=CAUSE_VOLCANIC, source_slug="erta-ale"
                )
            },
        )

    monkeypatch.setattr(granule, "granule_reduction", fake_reduction)
    monkeypatch.setattr(
        granule, "read_fire_granule", lambda path: (np.array([12.005]), np.array([34.005]))
    )

    day = cli.DayAccumulator()
    cli.run_product(
        StubSession(),
        "MOD11_L2",
        TARGET,
        "token",
        None,
        max_granules=None,
        dry_run=True,
        day=day,
        fire_masking=True,
    )

    assert list(day.anomalies) == ["MOD11_L2"]
    found = day.anomalies["MOD11_L2"]
    assert list(found) == [(13, 40, CAUSE_VOLCANIC)]
    assert found[(13, 40, CAUSE_VOLCANIC)].tile.max_c == 90.37
    assert found[(13, 40, CAUSE_VOLCANIC)].source_slug == "erta-ale"


def test_the_fire_mask_reaches_every_granule(patched_granule):
    session = StubSession(lst_count=2, fire_count=2)

    cli.run_product(
        session,
        "MOD11_L2",
        TARGET,
        "token",
        None,
        max_granules=None,
        dry_run=True,
        fire_masking=True,
    )

    assert len(patched_granule["exclusions"]) == 2
    assert all(keys is not None and keys.size == 9 for keys in patched_granule["exclusions"])
    assert patched_granule["notes"] == [QC_NOTE, QC_NOTE]


def test_a_day_without_fire_granules_marks_its_tiles_unchecked(patched_granule):
    session = StubSession(lst_count=2, fire_count=0)

    result = cli.run_product(
        session,
        "MOD11_L2",
        TARGET,
        "token",
        None,
        max_granules=None,
        dry_run=True,
        fire_masking=True,
    )

    assert result.ok
    assert patched_granule["exclusions"] == [None, None]
    assert all(note.endswith(FIRE_UNAVAILABLE_NOTE) for note in patched_granule["notes"])


# --- the raster stage ---------------------------------------------------------------


def populated_store() -> raster.TileStore:
    store: raster.TileStore = {}
    raster.accumulate_granule(
        store, np.array([61.5, 45.0]), np.array([0.0, 31.4]), np.array([0.0, -115.2])
    )
    return store


def test_dry_run_writes_the_bucket_layout_to_disk(tmp_path, capsys):
    tiles_dir = tmp_path / "out-tiles"

    assert cli.publish_raster(populated_store(), TARGET, None, True, tiles_dir)

    manifest = json.loads((tiles_dir / storage_io.MANIFEST_OBJECT).read_text())
    assert manifest["date"] == "2026-08-30"
    assert manifest["tile_url_template"] == "{date}/{z}/{x}/{y}.png"

    written = sorted(p.relative_to(tiles_dir).as_posix() for p in tiles_dir.rglob("*.png"))
    assert manifest["tile_count"] == len(written)
    # Two hot pixels, so two base tiles, and one world tile covering both.
    assert "2026-08-30/0/0/0.png" in written
    assert sum(1 for path in written if path.startswith("2026-08-30/7/")) == 2

    output = capsys.readouterr().out
    assert "z7:" in output and "total:" in output


def test_an_empty_day_publishes_no_tiles_and_is_not_a_failure(tmp_path):
    assert cli.publish_raster({}, TARGET, None, True, tmp_path)
    assert not list(tmp_path.rglob("*.png"))


def test_a_failed_raster_stage_is_reported_not_raised(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise MemoryError("the runner ran out of room")

    monkeypatch.setattr(raster, "build_pyramid", explode)

    assert cli.publish_raster(populated_store(), TARGET, None, True, tmp_path) is False


def test_the_manifest_is_published_only_after_the_tiles(monkeypatch, tmp_path):
    order: list[str] = []
    counts: dict[str, int] = {}

    class RecordingUploader:
        def __init__(self, *args, **kwargs):
            pass

        def upload_tiles(self, objects, cache_control=None):
            order.append("tiles")
            counts["uploaded"] = len(objects)
            counts["cache_control"] = cache_control
            return storage_io.UploadReport(uploaded=len(objects), failed=0)

        def upload_manifest(self, manifest):
            order.append("manifest")
            counts["promised"] = manifest["tile_count"]

        def prune_old_dates(self):
            order.append("prune")
            return 0

    monkeypatch.setattr(storage_io, "StorageUploader", RecordingUploader)

    assert cli.publish_raster(populated_store(), TARGET, "service-key", False, tmp_path)
    # Tiles first, then the manifest describing exactly what got there, then the
    # housekeeping that deletes older days.
    assert order == ["tiles", "manifest", "prune"]
    assert counts["promised"] == counts["uploaded"] > 0
    # Daily tiles are never republished with different content at the same
    # path, so they carry the long-lived immutable header.
    assert counts["cache_control"] == storage_io.TILE_CACHE_CONTROL


def test_losing_too_many_tiles_fails_the_stage(monkeypatch, tmp_path):
    class LosingUploader:
        def __init__(self, *args, **kwargs):
            pass

        def upload_tiles(self, objects, cache_control=None):
            return storage_io.UploadReport(uploaded=1, failed=len(objects) - 1)

        def upload_manifest(self, manifest):
            raise AssertionError("the manifest must not promise tiles that are not there")

        def prune_old_dates(self):
            raise AssertionError("nothing should be pruned after a failed upload")

    monkeypatch.setattr(storage_io, "StorageUploader", LosingUploader)

    assert cli.publish_raster(populated_store(), TARGET, "service-key", False, tmp_path) is False


def test_pruning_trouble_does_not_undo_a_good_day(monkeypatch, tmp_path):
    class PruneFailingUploader:
        def __init__(self, *args, **kwargs):
            pass

        def upload_tiles(self, objects, cache_control=None):
            return storage_io.UploadReport(uploaded=len(objects), failed=0)

        def upload_manifest(self, manifest):
            pass

        def prune_old_dates(self):
            raise RuntimeError("list timed out")

    monkeypatch.setattr(storage_io, "StorageUploader", PruneFailingUploader)

    assert cli.publish_raster(populated_store(), TARGET, "service-key", False, tmp_path)


def test_a_whole_dry_run_produces_rows_and_tiles_for_both_satellites(
    patched_granule, monkeypatch, tmp_path, capsys
):
    """main() end to end: both products reduced, one pyramid written."""
    import requests

    session = StubSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("EARTHDATA_TOKEN", "token")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)

    tiles_dir = tmp_path / "out-tiles"
    exit_code = cli.main(
        ["--date", "2026-08-30", "--dry-run", "--tiles-dir", str(tiles_dir)]
    )

    assert exit_code == 0
    assert session.writes == []

    # Four LST granules downloaded (two products, two overpasses each) and a
    # fire granule fetched alongside every one of them.
    assert len(session.downloads) == 8

    manifest = json.loads((tiles_dir / storage_io.MANIFEST_OBJECT).read_text())
    assert manifest["date"] == "2026-08-30"
    assert manifest["tile_count"] == len(list(tiles_dir.rglob("*.png")))
    assert manifest["tile_count"] > 0

    # Staged locally too: readings/anomalies per product, same row shape a
    # live upsert would send, under a per-date directory.
    day_dir = tiles_dir / "2026-08-30"
    mod_rows = json.loads((day_dir / "readings_MOD11_L2.json").read_text())
    myd_rows = json.loads((day_dir / "readings_MYD11_L2.json").read_text())
    assert mod_rows and myd_rows
    assert mod_rows[0]["reading_date"] == "2026-08-30"
    assert mod_rows[0]["satellite"] == "Terra"
    assert mod_rows[0]["product"] == "MOD11_L2"
    # No network round trip for names on a staged export -- backfilled later.
    assert mod_rows[0]["place_name"] is None

    output = capsys.readouterr().out
    assert "MOD11_L2 (Terra)" in output and "MYD11_L2 (Aqua)" in output


def test_the_tiles_directory_flag_is_parsed():
    args = cli.build_parser().parse_args(["--dry-run", "--tiles-dir", "/tmp/kiln-tiles"])
    assert str(args.tiles_dir) == "/tmp/kiln-tiles"
