"""Tests for CMR query building and response parsing. No network."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

import pytest

from kiln_ingest.cmr import (
    CMR_GRANULES_URL,
    MAX_PAGE_SIZE,
    NRT_PROVIDER,
    GranuleDiscoveryError,
    build_granule_query,
    build_granule_query_string,
    dedupe_granules,
    parse_granule_entries,
    satellite_for_product,
    temporal_range,
)

TARGET = date(2026, 8, 30)


def test_cmr_url_is_the_granule_search_endpoint():
    assert CMR_GRANULES_URL == "https://cmr.earthdata.nasa.gov/search/granules.json"


def test_satellite_mapping():
    assert satellite_for_product("MOD11_L2") == "Terra"
    assert satellite_for_product("MYD11_L2") == "Aqua"


def test_satellite_mapping_rejects_unknown_product():
    with pytest.raises(ValueError, match="unknown product"):
        satellite_for_product("MOD09GA")


def test_temporal_range_covers_the_whole_utc_day():
    assert temporal_range(TARGET) == "2026-08-30T00:00:00Z,2026-08-30T23:59:59Z"


def test_query_asks_for_daytime_nrt_granules():
    query = build_granule_query("MYD11_L2", TARGET)
    assert query["short_name"] == "MYD11_L2"
    assert query["provider"] == NRT_PROVIDER
    assert query["day_night_flag"] == "day"
    assert query["temporal"] == "2026-08-30T00:00:00Z,2026-08-30T23:59:59Z"
    assert query["page_size"] == MAX_PAGE_SIZE
    assert query["page_num"] == 1


def test_query_string_round_trips():
    parsed = parse_qs(build_granule_query_string("MOD11_L2", TARGET, page_size=50, page_num=3))
    assert parsed["short_name"] == ["MOD11_L2"]
    assert parsed["page_size"] == ["50"]
    assert parsed["page_num"] == ["3"]
    assert parsed["temporal"] == ["2026-08-30T00:00:00Z,2026-08-30T23:59:59Z"]


def test_query_rejects_out_of_range_paging():
    with pytest.raises(ValueError, match="page_size"):
        build_granule_query("MOD11_L2", TARGET, page_size=5000)
    with pytest.raises(ValueError, match="page_num"):
        build_granule_query("MOD11_L2", TARGET, page_num=0)


def entry(granule_id: str, href: str, **overrides):
    payload = {
        "producer_granule_id": granule_id,
        "time_start": "2026-08-30T11:25:00.000Z",
        "links": [
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#", "href": href},
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/metadata#", "href": href + ".xml"},
        ],
    }
    payload.update(overrides)
    return payload


def test_parse_extracts_id_url_and_time():
    feed = {"feed": {"entry": [
        entry("MOD11_L2.A2026242.1125.061.NRT.hdf",
              "https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/archives/"
              "MOD11_L2.A2026242.1125.061.NRT.hdf"),
    ]}}

    refs = parse_granule_entries(feed)

    assert len(refs) == 1
    assert refs[0].granule_id == "MOD11_L2.A2026242.1125.061.NRT.hdf"
    assert refs[0].url.startswith("https://nrt3.modaps.eosdis.nasa.gov/")
    assert refs[0].observed_at == "2026-08-30T11:25:00.000Z"


def test_parse_ignores_inherited_collection_links():
    feed = {"feed": {"entry": [{
        "producer_granule_id": "G1",
        "time_start": "2026-08-30T11:25:00.000Z",
        "links": [
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
             "href": "https://example.org/collection.hdf", "inherited": True},
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
             "href": "https://nrt4.modaps.eosdis.nasa.gov/real.hdf"},
        ],
    }]}}

    assert parse_granule_entries(feed)[0].url == "https://nrt4.modaps.eosdis.nasa.gov/real.hdf"


def test_parse_skips_entries_without_an_hdf_link():
    feed = {"feed": {"entry": [
        {"producer_granule_id": "no-links", "time_start": "2026-08-30T11:25:00.000Z",
         "links": [{"rel": "http://esipfed.org/ns/fedsearch/1.1/browse#",
                    "href": "https://example.org/thumb.jpg"}]},
        entry("G2", "https://nrt3.modaps.eosdis.nasa.gov/G2.hdf"),
    ]}}

    refs = parse_granule_entries(feed)
    assert [r.granule_id for r in refs] == ["G2"]


def test_parse_skips_entries_without_a_start_time():
    feed = {"feed": {"entry": [
        entry("G1", "https://nrt3.modaps.eosdis.nasa.gov/G1.hdf", time_start=None, updated=None),
    ]}}
    assert parse_granule_entries(feed) == []


def test_parse_rejects_a_body_with_no_feed():
    with pytest.raises(GranuleDiscoveryError, match="feed.entry"):
        parse_granule_entries({"errors": ["bad request"]})


def test_parse_handles_an_empty_day():
    assert parse_granule_entries({"feed": {"entry": []}}) == []


def test_dedupe_keeps_first_occurrence_in_order():
    feed = {"feed": {"entry": [
        entry("G1", "https://nrt3.modaps.eosdis.nasa.gov/G1.hdf"),
        entry("G2", "https://nrt3.modaps.eosdis.nasa.gov/G2.hdf"),
        entry("G1", "https://nrt4.modaps.eosdis.nasa.gov/G1.hdf"),
    ]}}
    refs = dedupe_granules(parse_granule_entries(feed))
    assert [r.granule_id for r in refs] == ["G1", "G2"]
    assert refs[0].url.startswith("https://nrt3.")
