"""CMR query construction and response parsing, with no network."""

from __future__ import annotations

from datetime import date

import pytest

from kiln_scan.cmr import (
    COLLECTION_VERSION,
    DATA_REL,
    GranuleDiscoveryError,
    build_granule_query,
    find_daily_granule,
    parse_granule_entries,
    product_info,
    temporal_range,
)

# Trimmed from the live CMR response for MOD11C1 on 2019-07-15, captured
# 2026-08-31. The link set is what the parser has to pick through: one HDF, a
# .cmr.xml sidecar under a rel that also ends in "data#", two S3 URIs, a browse
# JPEG, and an inherited collection-level link.
REAL_ENTRY = {
    "producer_granule_id": "MOD11C1.A2019196.061.2020356040840",
    "title": "MOD11C1.A2019196.061.2020356040840",
    "time_start": "2019-07-15T00:00:00.000Z",
    "time_end": "2019-07-15T23:59:59.000Z",
    "data_center": "LPCLOUD",
    "granule_size": "44.5221",
    "links": [
        {
            "rel": DATA_REL,
            "href": (
                "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/"
                "MOD11C1.061/MOD11C1.A2019196.061.2020356040840/"
                "MOD11C1.A2019196.061.2020356040840.hdf"
            ),
        },
        {
            "rel": "http://esipfed.org/ns/fedsearch/1.1/s3#",
            "href": (
                "s3://lp-prod-protected/MOD11C1.061/"
                "MOD11C1.A2019196.061.2020356040840/"
                "MOD11C1.A2019196.061.2020356040840.hdf"
            ),
        },
        {
            "rel": "http://esipfed.org/ns/fedsearch/1.1/metadata#",
            "href": (
                "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/"
                "MOD11C1.061/MOD11C1.A2019196.061.2020356040840/"
                "MOD11C1.A2019196.061.2020356040840.cmr.xml"
            ),
        },
        {
            "rel": "http://esipfed.org/ns/fedsearch/1.1/browse#",
            "href": (
                "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-public/"
                "MOD11C1.061/MOD11C1.A2019196.061.2020356040840/"
                "BROWSE.MOD11C1.A2019196.061.2020356040840.1.jpg"
            ),
        },
        {
            "inherited": True,
            "rel": DATA_REL,
            "href": "https://search.earthdata.nasa.gov/search/granules?p=C2565788888-LPCLOUD",
        },
    ],
}


def feed(*entries: dict) -> dict:
    return {"feed": {"entry": list(entries)}}


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.raised = False

    def raise_for_status(self) -> None:
        self.raised = True

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Records the query it was asked to run and replays a canned response."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict, timeout: int) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse(self.payload)


# --- Products -----------------------------------------------------------------------


def test_product_record_starts_match_the_missions():
    assert product_info("MOD11C1").record_start == date(2000, 2, 24)
    assert product_info("MOD11C1").satellite == "Terra"
    assert product_info("MYD11C1").record_start == date(2002, 7, 4)
    assert product_info("MYD11C1").satellite == "Aqua"


def test_an_unknown_product_is_refused():
    with pytest.raises(ValueError, match="unknown product"):
        product_info("MOD11_L2")


# --- Query building -----------------------------------------------------------------


def test_temporal_range_covers_the_whole_utc_day():
    assert temporal_range(date(2019, 7, 15)) == (
        "2019-07-15T00:00:00Z,2019-07-15T23:59:59Z"
    )


def test_query_names_the_product_and_collection():
    query = build_granule_query("MOD11C1", date(2019, 7, 15))
    assert query["short_name"] == "MOD11C1"
    assert query["version"] == COLLECTION_VERSION == "061"
    assert query["temporal"] == temporal_range(date(2019, 7, 15))


def test_query_carries_no_provider_filter():
    # The provider a collection is served from is an archive-side detail that
    # has moved before; filtering on it would turn a re-homing into a scan that
    # silently finds nothing.
    assert "provider" not in build_granule_query("MOD11C1", date(2019, 7, 15))


def test_query_rejects_an_unknown_product():
    with pytest.raises(ValueError):
        build_granule_query("MOD11A1", date(2019, 7, 15))


def test_query_rejects_a_nonsense_page_size():
    with pytest.raises(ValueError):
        build_granule_query("MOD11C1", date(2019, 7, 15), page_size=0)


# --- Response parsing ---------------------------------------------------------------


def test_parse_picks_the_https_hdf_link():
    (ref,) = parse_granule_entries(feed(REAL_ENTRY))
    assert ref.granule_id == "MOD11C1.A2019196.061.2020356040840"
    assert ref.url.startswith("https://data.lpdaac.earthdatacloud.nasa.gov/")
    assert ref.url.endswith(".hdf")
    assert ref.time_start == "2019-07-15T00:00:00.000Z"


def test_parse_ignores_the_metadata_sidecar():
    # "metadata#" ends in "data#". A suffix test on the relation would hand back
    # the .cmr.xml file, and the scan would try to parse XML as HDF4.
    (ref,) = parse_granule_entries(feed(REAL_ENTRY))
    assert ".cmr.xml" not in ref.url


def test_parse_ignores_s3_uris_and_browse_images():
    (ref,) = parse_granule_entries(feed(REAL_ENTRY))
    assert not ref.url.startswith("s3://")
    assert "BROWSE" not in ref.url


def test_parse_ignores_inherited_collection_level_links():
    entry = {
        "producer_granule_id": "MOD11C1.A2019196.061.2020356040840",
        "links": [
            {"inherited": True, "rel": DATA_REL, "href": "https://example.invalid/x.hdf"}
        ],
    }
    assert parse_granule_entries(feed(entry)) == []


def test_parse_skips_an_entry_with_no_data_link():
    entry = {"producer_granule_id": "MOD11C1.A2019196.061.x", "links": []}
    assert parse_granule_entries(feed(entry, REAL_ENTRY)) == parse_granule_entries(
        feed(REAL_ENTRY)
    )


def test_parse_deduplicates_repeated_granule_ids():
    assert len(parse_granule_entries(feed(REAL_ENTRY, dict(REAL_ENTRY)))) == 1


def test_parse_of_an_empty_day_is_empty_not_an_error():
    assert parse_granule_entries(feed()) == []


def test_parse_without_a_feed_is_an_error():
    with pytest.raises(GranuleDiscoveryError, match="feed.entry"):
        parse_granule_entries({"errors": ["bad query"]})


# --- Lookup -------------------------------------------------------------------------


def test_find_daily_granule_returns_the_day_s_file():
    session = FakeSession(feed(REAL_ENTRY))
    ref = find_daily_granule(session, "MOD11C1", date(2019, 7, 15))

    assert ref is not None
    assert ref.granule_id == "MOD11C1.A2019196.061.2020356040840"
    url, params = session.calls[0]
    assert url.endswith("/search/granules.json")
    assert params["short_name"] == "MOD11C1"


def test_find_daily_granule_returns_none_for_a_day_with_no_data():
    # 2000-02-29 has no MOD11C1 granule. An absent day is a normal state of this
    # record, so it is a return value rather than an exception.
    session = FakeSession(feed())
    assert find_daily_granule(session, "MOD11C1", date(2000, 2, 29)) is None


def test_find_daily_granule_takes_the_first_of_a_reprocessing_overlap():
    older = dict(REAL_ENTRY)
    newer = dict(REAL_ENTRY, producer_granule_id="MOD11C1.A2019196.061.2021999999999")
    session = FakeSession(feed(older, newer))
    ref = find_daily_granule(session, "MOD11C1", date(2019, 7, 15))
    assert ref.granule_id == "MOD11C1.A2019196.061.2020356040840"
