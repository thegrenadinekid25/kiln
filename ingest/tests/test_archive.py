"""Archive discovery: LP DAAC holdings, spatial filters, and granule naming.

The provider and query shapes here were verified against live CMR metadata on
2026-08-31 and are pinned so a change in either fails loudly rather than
silently returning an empty backfill.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

import pytest

from kiln_ingest.cli import Discovery, build_parser, parse_bbox
from kiln_ingest.cmr import (
    ARCHIVE_PROVIDER,
    ARCHIVE_VERSION,
    NRT_PROVIDER,
    build_fire_query,
    build_granule_query,
    build_granule_query_string,
    fire_product_for,
    granule_time_key,
    product_from_granule_id,
    time_key_map,
)

TARGET = date(2019, 7, 15)
LUT = (55.0, 28.0, 62.0, 33.0)
MOJAVE = (-118.0, 32.0, -114.0, 36.0)

# Real granule ids from both feeds. The archive names end in a production
# timestamp where the NRT names carry the literal NRT.
ARCHIVE_LST = "MOD11_L2.A2019196.0635.061.2020356013308.hdf"
ARCHIVE_FIRE = "MOD14.A2019196.0635.061.2020302063032.hdf"
NRT_LST = "MOD11_L2.A2026242.1125.061.NRT.hdf"
NRT_FIRE = "MOD14.A2026242.1125.061.NRT.hdf"


# --- which holdings ------------------------------------------------------------------


def test_the_daily_run_still_asks_lance_and_pins_no_version():
    query = build_granule_query("MOD11_L2", TARGET)
    assert query["provider"] == NRT_PROVIDER == "LANCEMODIS"
    assert "version" not in query


def test_the_archive_run_asks_lpcloud_for_collection_061():
    query = build_granule_query("MOD11_L2", TARGET, archive=True)
    assert query["provider"] == ARCHIVE_PROVIDER == "LPCLOUD"
    assert query["version"] == ARCHIVE_VERSION == "061"


def test_the_archive_keeps_every_other_filter_the_daily_run_uses():
    nrt = build_granule_query("MYD11_L2", TARGET)
    archive = build_granule_query("MYD11_L2", TARGET, archive=True)
    for key in ("short_name", "temporal", "day_night_flag", "sort_key"):
        assert nrt[key] == archive[key]


def test_the_fire_query_follows_the_lst_query_into_the_archive():
    # A backfill that masked fires against the NRT feed would find nothing, and
    # every tile would silently come back marked unchecked.
    query = build_fire_query(fire_product_for("MOD11_L2"), TARGET, archive=True)
    assert query["short_name"] == "MOD14"
    assert query["provider"] == ARCHIVE_PROVIDER
    assert query["version"] == ARCHIVE_VERSION
    assert "day_night_flag" not in query


# --- spatial filters -----------------------------------------------------------------


def test_no_bounding_box_means_the_whole_globe():
    assert "bounding_box" not in build_granule_query("MOD11_L2", TARGET, archive=True)


def test_one_box_is_west_south_east_north():
    query = build_granule_query("MOD11_L2", TARGET, archive=True, bboxes=[LUT])
    assert query["bounding_box"] == ["55,28,62,33"]
    # A single box needs no combining rule.
    assert "options[bounding_box][or]" not in query


def test_negative_and_fractional_degrees_survive_formatting():
    query = build_granule_query(
        "MOD11_L2", TARGET, archive=True, bboxes=[(-118.25, 32.5, -114.0, 36.125)]
    )
    assert query["bounding_box"] == ["-118.25,32.5,-114,36.125"]


def test_several_boxes_are_ored_not_anded():
    # Verified against CMR: two disjoint boxes return nothing without this
    # option and both granules with it, because repeated spatial filters are
    # ANDed by default.
    query = build_granule_query("MOD11_L2", TARGET, archive=True, bboxes=[LUT, MOJAVE])
    assert query["bounding_box"] == ["55,28,62,33", "-118,32,-114,36"]
    assert query["options[bounding_box][or]"] == "true"


def test_the_fire_query_is_narrowed_to_the_same_regions():
    query = build_fire_query("MOD14", TARGET, archive=True, bboxes=[LUT, MOJAVE])
    assert query["bounding_box"] == ["55,28,62,33", "-118,32,-114,36"]
    assert query["options[bounding_box][or]"] == "true"


def test_the_query_string_repeats_the_box_parameter():
    parsed = parse_qs(
        build_granule_query_string("MOD11_L2", TARGET, archive=True, bboxes=[LUT, MOJAVE])
    )
    assert parsed["bounding_box"] == ["55,28,62,33", "-118,32,-114,36"]
    assert parsed["provider"] == ["LPCLOUD"]
    assert parsed["version"] == ["061"]
    assert parsed["options[bounding_box][or]"] == ["true"]


def test_the_daily_query_string_is_unchanged_by_any_of_this():
    parsed = parse_qs(build_granule_query_string("MOD11_L2", TARGET))
    assert parsed["provider"] == ["LANCEMODIS"]
    assert "bounding_box" not in parsed
    assert "version" not in parsed


# --- granule naming across both feeds ------------------------------------------------


@pytest.mark.parametrize("granule_id,stamp", [
    (ARCHIVE_LST, "A2019196.0635"),
    (ARCHIVE_FIRE, "A2019196.0635"),
    (NRT_LST, "A2026242.1125"),
    (NRT_FIRE, "A2026242.1125"),
    ("MYD11_L2.A2000065.0730.061.2020043004435.hdf", "A2000065.0730"),
])
def test_the_overpass_stamp_reads_the_same_in_both_feeds(granule_id, stamp):
    assert granule_time_key(granule_id) == stamp


def test_an_archive_lst_granule_pairs_with_its_archive_fire_granule():
    assert granule_time_key(ARCHIVE_LST) == granule_time_key(ARCHIVE_FIRE)


@pytest.mark.parametrize("granule_id,product", [
    (ARCHIVE_LST, "MOD11_L2"),
    (NRT_LST, "MOD11_L2"),
    ("MYD11_L2.A2019196.0945.061.2020356005133.hdf", "MYD11_L2"),
])
def test_the_product_reads_the_same_in_both_feeds(granule_id, product):
    assert product_from_granule_id(granule_id) == product


def test_a_fire_granule_names_no_lst_product():
    assert product_from_granule_id(ARCHIVE_FIRE) is None


def test_archive_granules_map_to_urls_by_stamp():
    from kiln_ingest.cmr import GranuleRef

    refs = [
        GranuleRef(ARCHIVE_FIRE, "https://data.lpdaac.earthdatacloud.nasa.gov/a.hdf", "t"),
        GranuleRef(
            "MOD14.A2019196.0810.061.2020302063033.hdf",
            "https://data.lpdaac.earthdatacloud.nasa.gov/b.hdf",
            "t",
        ),
    ]
    assert sorted(time_key_map(refs)) == ["A2019196.0635", "A2019196.0810"]


# --- the command line ----------------------------------------------------------------


def test_the_daily_run_needs_no_new_flags():
    args = build_parser().parse_args([])
    assert args.archive is False
    assert args.bbox is None
    assert Discovery(archive=args.archive, bboxes=tuple(args.bbox or ())) == Discovery()


def test_a_backfill_reads_its_flags():
    args = build_parser().parse_args(
        ["--archive", "--date", "2019-07-15", "--bbox", "55,28,62,33"]
    )
    assert args.archive is True
    assert args.bbox == [LUT]
    assert args.date == TARGET


def test_bbox_repeats_into_a_list():
    args = build_parser().parse_args(
        ["--bbox", "55,28,62,33", "--bbox=-118,32,-114,36"]
    )
    assert args.bbox == [LUT, MOJAVE]


def test_a_western_box_needs_the_equals_form():
    """A leading minus reads as a flag, so the help says to write --bbox=-118,...

    Documented as a test because the failure is otherwise baffling: argparse
    reports "expected one argument" for what looks like a perfectly good value.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--bbox", "-118,32,-114,36"])

    assert build_parser().parse_args(["--bbox=-118,32,-114,36"]).bbox == [MOJAVE]
    assert "--bbox=-118,32,-114,36" in build_parser().format_help()


def test_a_box_crossing_the_antimeridian_is_allowed():
    # CMR reads west > east as a box wrapping past 180, which is a real case.
    assert parse_bbox("170,-10,-170,10") == (170.0, -10.0, -170.0, 10.0)


@pytest.mark.parametrize("value,message", [
    ("55,28,62", "four comma-separated"),
    ("55,28,62,33,40", "four comma-separated"),
    ("", "four comma-separated"),
    ("a,b,c,d", "must be numbers"),
    ("55,28,62,north", "must be numbers"),
    ("200,28,62,33", "west must be within"),
    ("55,-91,62,33", "south must be within"),
    ("55,28,62,91", "north must be within"),
    ("55,33,62,28", "south .* must be below north"),
    ("55,33,62,33", "south .* must be below north"),
])
def test_a_malformed_box_is_rejected_with_a_usable_message(value, message):
    with pytest.raises(Exception, match=message):
        parse_bbox(value)


def test_the_discovery_bundle_names_which_holdings_it_searched():
    assert Discovery().holdings == "LANCE near-real-time"
    assert Discovery(archive=True).holdings == "LP DAAC archive"
