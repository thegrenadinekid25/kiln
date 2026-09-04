"""Direct-S3 downloads: credential parsing, self-disable, and the HTTPS
fallback in :func:`download_granule_auto`. No network, no real boto3 client --
the actual S3 read is region-gated and can only be exercised from AWS
us-west-2 (verified by hand against the real endpoint on 2026-09-03).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiln_ingest.cmr import GranuleRef
from kiln_ingest.download import (
    S3_REFRESH_MARGIN,
    DownloadError,
    S3Credentials,
    S3Fetcher,
    download_granule_auto,
    parse_s3_url,
)

TOKEN = "not-a-real-token"


class StubSession:
    """A session whose .get() is never expected to be called by these tests."""

    def get(self, *args, **kwargs):
        raise AssertionError("no network access expected in these tests")


# --- parse_s3_url ---------------------------------------------------------------------


def test_parse_s3_url_splits_bucket_and_key():
    bucket, key = parse_s3_url("s3://lp-prod-protected/MOD11_L2.061/x/g.hdf")
    assert bucket == "lp-prod-protected"
    assert key == "MOD11_L2.061/x/g.hdf"


@pytest.mark.parametrize("bad", [
    "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/g.hdf",
    "s3://",
    "s3:///no-bucket/g.hdf",
    "not-a-url",
])
def test_parse_s3_url_rejects_anything_not_a_bucket_and_key(bad):
    with pytest.raises(DownloadError):
        parse_s3_url(bad)


# --- S3Credentials.stale ---------------------------------------------------------------


def test_credentials_are_stale_once_inside_the_refresh_margin():
    almost_expired = S3Credentials(
        access_key_id="a",
        secret_access_key="b",
        session_token="c",
        expires_at=datetime.now(timezone.utc) + (S3_REFRESH_MARGIN / 2),
    )
    assert almost_expired.stale


def test_credentials_are_fresh_well_before_expiry():
    fresh = S3Credentials(
        access_key_id="a",
        secret_access_key="b",
        session_token="c",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert not fresh.stale


# --- S3Fetcher self-disable -------------------------------------------------------------


def test_a_disabled_fetcher_refuses_to_download(tmp_path):
    fetcher = S3Fetcher(StubSession(), TOKEN)
    fetcher.disable("test disable")
    with pytest.raises(DownloadError):
        fetcher.download("s3://lp-prod-protected/x.hdf", tmp_path / "out.hdf")


def test_disable_sets_the_flag_and_logs(caplog):
    fetcher = S3Fetcher(StubSession(), TOKEN)
    assert not fetcher.disabled
    with caplog.at_level("WARNING"):
        fetcher.disable("403 from outside us-west-2")
    assert fetcher.disabled
    assert "403 from outside us-west-2" in caplog.text


# --- download_granule_auto fallback -----------------------------------------------------


REF = GranuleRef(
    granule_id="MOD11_L2.A2020196.2355.061.x.hdf",
    url="https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/x.hdf",
    observed_at="2020-07-14T23:55:00.000Z",
    s3_url="s3://lp-prod-protected/x.hdf",
)


def test_no_fetcher_goes_straight_to_https(tmp_path, monkeypatch):
    calls = []

    def fake_https(session, url, destination, token, attempts=3):
        calls.append(url)
        destination.write_bytes(b"data")
        return destination

    monkeypatch.setattr("kiln_ingest.download.download_granule", fake_https)
    destination = tmp_path / "out.hdf"

    download_granule_auto(StubSession(), REF, destination, TOKEN, s3_fetcher=None)

    assert calls == [REF.url]
    assert destination.read_bytes() == b"data"


def test_a_ref_with_no_s3_url_goes_straight_to_https_even_with_a_fetcher(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "kiln_ingest.download.download_granule",
        lambda session, url, destination, token, attempts=3: (calls.append(url), destination.write_bytes(b"d"))[1] or destination,
    )
    no_s3_ref = GranuleRef(granule_id="g", url="https://x/g.hdf", observed_at="t", s3_url=None)
    fetcher = S3Fetcher(StubSession(), TOKEN)

    download_granule_auto(StubSession(), no_s3_ref, tmp_path / "out.hdf", TOKEN, s3_fetcher=fetcher)

    assert calls == ["https://x/g.hdf"]
    assert not fetcher.disabled  # never even tried, so never disabled


def test_a_failing_s3_attempt_disables_the_fetcher_and_falls_back_to_https(tmp_path, monkeypatch):
    def explode(self, s3_url, destination):
        raise DownloadError("403 Forbidden")

    monkeypatch.setattr(S3Fetcher, "download", explode)

    calls = []

    def fake_https(session, url, destination, token, attempts=3):
        calls.append(url)
        destination.write_bytes(b"data")
        return destination

    monkeypatch.setattr("kiln_ingest.download.download_granule", fake_https)
    fetcher = S3Fetcher(StubSession(), TOKEN)
    destination = tmp_path / "out.hdf"

    result = download_granule_auto(StubSession(), REF, destination, TOKEN, s3_fetcher=fetcher)

    assert result == destination
    assert calls == [REF.url]
    assert fetcher.disabled


def test_a_successful_s3_download_never_touches_https(tmp_path, monkeypatch):
    def fake_s3(self, s3_url, destination):
        destination.write_bytes(b"from-s3")
        return destination

    monkeypatch.setattr(S3Fetcher, "download", fake_s3)
    monkeypatch.setattr(
        "kiln_ingest.download.download_granule",
        lambda *a, **k: pytest.fail("HTTPS should not be reached when S3 succeeds"),
    )
    fetcher = S3Fetcher(StubSession(), TOKEN)
    destination = tmp_path / "out.hdf"

    result = download_granule_auto(StubSession(), REF, destination, TOKEN, s3_fetcher=fetcher)

    assert result == destination
    assert destination.read_bytes() == b"from-s3"
    assert not fetcher.disabled


def test_once_disabled_a_fetcher_stays_on_https_for_the_rest_of_the_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "kiln_ingest.download.download_granule",
        lambda session, url, destination, token, attempts=3: (calls.append(url), destination.write_bytes(b"d"))[1] or destination,
    )
    fetcher = S3Fetcher(StubSession(), TOKEN)
    fetcher.disable("first granule 403'd")

    download_granule_auto(StubSession(), REF, tmp_path / "a.hdf", TOKEN, s3_fetcher=fetcher)
    download_granule_auto(StubSession(), REF, tmp_path / "b.hdf", TOKEN, s3_fetcher=fetcher)

    assert calls == [REF.url, REF.url]
