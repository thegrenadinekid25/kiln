"""Where the Earthdata token may travel. No network.

An archive download bounces off NASA's distribution host onto a pre-signed
CloudFront URL. The token has to survive a NASA-to-NASA hop that ``requests``
would strip on its own, and must not survive the hop off NASA -- so the decision
is made per hop, by one small function, with the default being no.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln_ingest.download import (
    MAX_REDIRECTS,
    DownloadError,
    bearer_allowed,
    download_granule,
)

TOKEN = "not-a-real-token"

ARCHIVE_URL = (
    "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/"
    "MOD11_L2.061/MOD11_L2.A2019196.0635.061.2020356013308.hdf"
)
CLOUDFRONT_URL = "https://d1nklfio7vscoe.cloudfront.net/lp-prod-protected/x.hdf?Signature=abc"


# --- the host policy ----------------------------------------------------------------


@pytest.mark.parametrize("url", [
    ARCHIVE_URL,
    "https://urs.earthdata.nasa.gov/oauth/authorize",
    "https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/archives/g.hdf",
    "https://nasa.gov/file.hdf",
    "https://NASA.GOV/file.hdf",
    "https://DATA.LPDAAC.EARTHDATACLOUD.NASA.GOV/x.hdf",
])
def test_nasa_hosts_may_carry_the_token(url):
    assert bearer_allowed(url)


@pytest.mark.parametrize("url,why", [
    (CLOUDFRONT_URL, "the pre-signed hop, which needs no token and must not get one"),
    ("https://evil-nasa.gov/steal", "a lookalike host that merely ends in the same letters"),
    ("https://nasa.gov.attacker.com/steal", "the real domain used as a prefix"),
    ("https://notnasa.gov/steal", "another suffix lookalike"),
    ("http://data.lpdaac.earthdatacloud.nasa.gov/x.hdf", "plaintext, which would leak it"),
    ("https://example.com/x.hdf", "an unrelated host"),
    ("https://user:pass@example.com/x.hdf", "credentials in the URL do not make it NASA"),
    ("ftp://data.nasa.gov/x.hdf", "a non-HTTPS scheme"),
    ("", "no URL at all"),
    ("not a url", "unparseable input"),
])
def test_everything_else_is_refused(url, why):
    assert not bearer_allowed(url), why


# --- following redirects ------------------------------------------------------------


class Hop:
    def __init__(self, status_code=200, location=None, body=b"granule-bytes"):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingSession:
    """Replays a scripted redirect chain, recording the headers of every hop."""

    def __init__(self, hops):
        self._hops = list(hops)
        self.seen: list[tuple[str, bool]] = []

    def get(self, url, headers=None, **kwargs):
        assert kwargs.get("allow_redirects") is False, "redirects must be followed by hand"
        self.seen.append((url, "Authorization" in (headers or {})))
        return self._hops.pop(0)


def test_a_single_host_download_carries_the_token(tmp_path):
    session = RecordingSession([Hop()])

    download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN)

    assert session.seen == [(ARCHIVE_URL, True)]
    assert (tmp_path / "g.hdf").read_bytes() == b"granule-bytes"


def test_the_token_survives_a_nasa_to_nasa_redirect(tmp_path):
    # requests would drop it here on its own, because the host changed.
    edl = "https://urs.earthdata.nasa.gov/oauth/authorize?client_id=x"
    session = RecordingSession([Hop(302, location=edl), Hop()])

    download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN)

    assert session.seen == [(ARCHIVE_URL, True), (edl, True)]


def test_the_token_is_dropped_at_the_cloudfront_hop(tmp_path):
    session = RecordingSession([Hop(303, location=CLOUDFRONT_URL), Hop()])

    download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN)

    assert session.seen == [(ARCHIVE_URL, True), (CLOUDFRONT_URL, False)]
    assert (tmp_path / "g.hdf").read_bytes() == b"granule-bytes"


def test_the_token_comes_back_when_a_redirect_returns_to_nasa(tmp_path):
    """The decision is per hop, not sticky: off NASA drops it, back on restores it."""
    session = RecordingSession([
        Hop(303, location=CLOUDFRONT_URL),
        Hop(307, location=ARCHIVE_URL),
        Hop(),
    ])

    download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN)

    assert [carried for _, carried in session.seen] == [True, False, True]


def test_a_relative_location_is_resolved_against_the_current_url(tmp_path):
    session = RecordingSession([Hop(302, location="/elsewhere/g.hdf"), Hop()])

    download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN)

    assert session.seen[1][0] == (
        "https://data.lpdaac.earthdatacloud.nasa.gov/elsewhere/g.hdf"
    )
    assert session.seen[1][1] is True


def test_a_redirect_off_nasa_via_a_relative_path_still_drops_the_token(tmp_path):
    session = RecordingSession([
        Hop(302, location="https://cdn.example.com/g.hdf"),
        Hop(),
    ])

    download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN)

    assert session.seen[1] == ("https://cdn.example.com/g.hdf", False)


def test_a_redirect_without_a_location_is_an_error(tmp_path):
    session = RecordingSession([Hop(302, location=None)] * 3)

    with pytest.raises(DownloadError, match="no Location"):
        download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN, attempts=1)


def test_an_endless_redirect_loop_is_cut_off(tmp_path):
    session = RecordingSession([Hop(302, location=ARCHIVE_URL)] * (MAX_REDIRECTS + 5))

    with pytest.raises(DownloadError, match="redirects"):
        download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN, attempts=1)


def test_an_empty_body_is_not_mistaken_for_a_granule(tmp_path):
    session = RecordingSession([Hop(body=b"")])

    with pytest.raises(DownloadError, match="empty body"):
        download_granule(session, ARCHIVE_URL, tmp_path / "g.hdf", TOKEN, attempts=1)

    assert not (tmp_path / "g.hdf").exists()


def test_a_failed_download_leaves_no_partial_file_behind(tmp_path):
    session = RecordingSession([Hop(status_code=503)])
    destination = Path(tmp_path / "g.hdf")

    with pytest.raises(DownloadError):
        download_granule(session, ARCHIVE_URL, destination, TOKEN, attempts=1)

    assert list(tmp_path.iterdir()) == []
