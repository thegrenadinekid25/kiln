"""Orchestration tests driven by a stub HTTP session. No network, no HDF files."""

from __future__ import annotations

from datetime import date

import pytest

from kiln_ingest import cli, granule
from kiln_ingest.science import TileMax
from kiln_ingest.supabase_io import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCEEDED,
    SupabaseWriteError,
    SupabaseWriter,
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


class StubSession:
    """Answers CMR searches, granule downloads and PostgREST writes."""

    def __init__(self, cmr_payload, download_status=200):
        self.cmr_payload = cmr_payload
        self.download_status = download_status
        self.downloads: list[str] = []
        self.writes: list[tuple[str, str, dict]] = []

    def get(self, url, **kwargs):
        if "cmr.earthdata.nasa.gov" in url:
            return StubResponse(payload=self.cmr_payload)
        self.downloads.append(url)
        return StubResponse(status_code=self.download_status)

    def request(self, method, url, **kwargs):
        self.writes.append((method, url, kwargs))
        if url.endswith("ingest_runs"):
            return StubResponse(payload=[{"id": 77}])
        return StubResponse(payload=[])


def cmr_feed(count: int):
    return {"feed": {"entry": [
        {
            "producer_granule_id": f"MOD11_L2.A2026242.{1100 + i}.061.NRT.hdf",
            "time_start": f"2026-08-30T11:{i:02d}:00.000Z",
            "links": [{
                "rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                "href": f"https://nrt3.modaps.eosdis.nasa.gov/G{i}.hdf",
            }],
        }
        for i in range(count)
    ]}}


def fake_maxima(hot_c: float):
    def _maxima(path, granule_id, observed_at):
        return {
            (31, -115): TileMax(31, -115, hot_c, 31.4, -115.2, observed_at, granule_id),
            (0, 0): TileMax(0, 0, 12.0, 0.5, 0.5, observed_at, granule_id),
        }
    return _maxima


@pytest.fixture
def patched_reader(monkeypatch):
    def install(fn):
        monkeypatch.setattr(granule, "granule_maxima", fn)
    return install


# --- argument handling --------------------------------------------------------------


def test_parser_defaults_to_both_products_and_no_date():
    args = cli.build_parser().parse_args([])
    assert args.date is None
    assert args.product is None
    assert args.dry_run is False


def test_parser_reads_the_documented_flags():
    args = cli.build_parser().parse_args(
        ["--date", "2026-08-30", "--product", "MYD11_L2", "--max-granules", "3", "--dry-run"]
    )
    assert args.date == TARGET
    assert args.product == "MYD11_L2"
    assert args.max_granules == 3
    assert args.dry_run is True


def test_parser_rejects_a_malformed_date():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--date", "30-08-2026"])


def test_parser_rejects_an_unknown_product():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--product", "MOD09GA"])


# --- run_product --------------------------------------------------------------------


def test_dry_run_writes_nothing_and_reports_tiles(patched_reader, capsys):
    patched_reader(fake_maxima(58.0))
    session = StubSession(cmr_feed(2))

    result = cli.run_product(
        session, "MOD11_L2", TARGET, "token", None, max_granules=None, dry_run=True
    )

    assert result.status == STATUS_SUCCEEDED
    assert result.granules_total == 2
    assert result.granules_processed == 2
    assert result.tiles_written == 0
    assert session.writes == []
    assert "58.00 C" in capsys.readouterr().out


def test_successful_run_upserts_and_closes_the_run_row(patched_reader):
    patched_reader(fake_maxima(58.0))
    session = StubSession(cmr_feed(2))

    result = cli.run_product(
        session, "MOD11_L2", TARGET, "token", "service-key", max_granules=None, dry_run=False
    )

    assert result.status == STATUS_SUCCEEDED
    # Both tiles are stored: one clears 40 C, the other rides in on the top-10 rule.
    assert result.tiles_written == 2

    methods = [(m, u.split("/rest/v1/")[1].split("?")[0]) for m, u, _ in session.writes]
    assert methods == [
        ("POST", "ingest_runs"),
        ("POST", "lst_readings"),
        ("PATCH", "ingest_runs"),
    ]

    _, upsert_url, upsert_kwargs = session.writes[1]
    assert "on_conflict=reading_date,product,tile_lat,tile_lon" in upsert_url
    assert upsert_kwargs["headers"]["Prefer"] == "resolution=merge-duplicates,return=minimal"
    assert upsert_kwargs["headers"]["Content-Profile"] == "kiln"
    assert {row["product"] for row in upsert_kwargs["json"]} == {"MOD11_L2"}
    assert {row["satellite"] for row in upsert_kwargs["json"]} == {"Terra"}

    _, patch_url, patch_kwargs = session.writes[2]
    assert patch_url.endswith("ingest_runs?id=eq.77")
    assert patch_kwargs["json"]["status"] == STATUS_SUCCEEDED
    assert patch_kwargs["json"]["granules_processed"] == 2


def test_max_granules_caps_downloads(patched_reader):
    patched_reader(fake_maxima(58.0))
    session = StubSession(cmr_feed(10))

    result = cli.run_product(
        session, "MOD11_L2", TARGET, "token", None, max_granules=3, dry_run=True
    )

    assert result.granules_total == 3
    assert len(session.downloads) == 3


def test_one_bad_granule_yields_partial_not_failed(patched_reader):
    calls = {"n": 0}

    def flaky(path, granule_id, observed_at):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("corrupt SDS")
        return fake_maxima(58.0)(path, granule_id, observed_at)

    patched_reader(flaky)
    session = StubSession(cmr_feed(3))

    result = cli.run_product(
        session, "MOD11_L2", TARGET, "token", "service-key", max_granules=None, dry_run=False
    )

    assert result.status == STATUS_PARTIAL
    assert result.granules_processed == 2
    assert result.granules_total == 3
    assert result.ok


def test_every_granule_failing_is_a_failed_run(patched_reader):
    def always_bad(path, granule_id, observed_at):
        raise ValueError("corrupt SDS")

    patched_reader(always_bad)
    session = StubSession(cmr_feed(2))

    result = cli.run_product(
        session, "MOD11_L2", TARGET, "token", "service-key", max_granules=None, dry_run=False
    )

    assert result.status == STATUS_FAILED
    assert result.granules_processed == 0
    assert "every MOD11_L2 granule failed" in result.error
    assert not result.ok
    # The run row is still closed with the error recorded.
    assert session.writes[-1][0] == "PATCH"
    assert session.writes[-1][2]["json"]["status"] == STATUS_FAILED


def test_an_empty_day_from_cmr_is_a_failed_run(patched_reader):
    patched_reader(fake_maxima(58.0))
    session = StubSession(cmr_feed(0))

    result = cli.run_product(
        session, "MOD11_L2", TARGET, "token", None, max_granules=None, dry_run=True
    )

    assert result.status == STATUS_FAILED
    assert "no daytime MOD11_L2 granules" in result.error


# --- SupabaseWriter transport -------------------------------------------------------


def test_writer_requires_a_service_key():
    with pytest.raises(SupabaseWriteError, match="SUPABASE_SERVICE_KEY"):
        SupabaseWriter(StubSession(cmr_feed(0)), "")


def test_writer_surfaces_a_rejected_write():
    class Rejecting(StubSession):
        def request(self, method, url, **kwargs):
            return StubResponse(status_code=401, text="invalid api key")

    writer = SupabaseWriter(Rejecting(cmr_feed(0)), "bad-key")
    with pytest.raises(SupabaseWriteError, match="401"):
        writer.start_run(TARGET, "MOD11_L2")


# --- main() guards ------------------------------------------------------------------


def test_main_refuses_to_run_without_an_earthdata_token(monkeypatch):
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    assert cli.main(["--dry-run"]) == 2


def test_main_refuses_a_live_run_without_a_service_key(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "token")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    assert cli.main([]) == 2


def test_main_records_a_failure_that_escapes_run_product(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "token")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")

    def exploding(*args, **kwargs):
        raise ConnectionError("supabase unreachable")

    monkeypatch.setattr(cli, "run_product", exploding)

    # Both products blow up before a run row exists, so the process exits 1
    # rather than dying with a traceback.
    assert cli.main(["--date", "2026-08-30"]) == 1
