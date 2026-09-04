"""Anomaly routing: heat that is real but is not weather (decision 2026-09-02).

The weather archive holds corroborated weather and nothing else. Four kinds of
reading are routed out of it into ``anomaly_readings`` instead of contaminating
it or being silently discarded: volcanic vents, notable wildfires, record-tier
readings the other satellite contradicted, and record-tier readings no second
satellite saw at all.

The motivating case is the archive's own top entry: 90.37 C at 13.59 N 40.67 E,
which is Erta Ale's lava lake. Cross-satellite corroboration passes it, and
passes it correctly -- a lava lake is hot on every overpass, which is exactly
what that screen is built to believe. Only a named list of vents can tell a lake
from a desert.

Pure functions throughout: no network, no HDF, no database.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from kiln_ingest import alltime, cli, raster
from kiln_ingest.science import (
    ACTIVE_FIRE_ANOMALY_NOTE,
    CAUSE_FAILED_CORROBORATION,
    CAUSE_UNCORROBORATED,
    CAUSE_VOLCANIC,
    CAUSE_WILDFIRE,
    CORROBORATION_THRESHOLD_C,
    KM_PER_DEGREE,
    QC_NOTE,
    VOLCANIC_ANOMALY_MIN_C,
    VOLCANIC_ANOMALY_NOTE,
    VOLCANIC_MASKED_NOTE,
    WILDFIRE_ANOMALY_MIN_C,
    Anomaly,
    TileMax,
    VolcanicSource,
    corroborate_day,
    fire_exclusion_keys,
    granule_anomalies,
    load_volcanic_sources,
    merge_anomalies,
    nearest_vent,
    parse_volcanic_sources,
    prepare_granule,
    tile_maxima,
    vents_in_range,
    volcanic_excluded_mask,
    volcanic_vent_indices,
)
from kiln_ingest.supabase_io import build_anomaly_row

GOOD_ATTRS = {"scale_factor": 0.02, "add_offset": 0.0, "_FillValue": 0}
KELVIN_ZERO_C = 273.15

TERRA = "MOD11_L2"
AQUA = "MYD11_L2"
OBSERVED_AT = "2026-08-30T08:20:00Z"
GRANULE = "MOD11_L2.A2026242.0820.061.NRT.hdf"

# The vent that motivated the screen, and one far enough away to test that a
# list of several picks the right one.
ERTA_ALE = VolcanicSource(
    slug="erta-ale",
    name="Erta Ale",
    country="Ethiopia",
    lat=13.60,
    lon=40.67,
    radius_km=7.0,
    source_name="Smithsonian Institution Global Volcanism Program",
    source_url="https://volcano.si.edu/",
)
KILAUEA = VolcanicSource(
    slug="kilauea",
    name="Kilauea",
    country="United States",
    lat=19.41,
    lon=-155.29,
    radius_km=10.0,
    source_name="Smithsonian Institution Global Volcanism Program",
    source_url="https://volcano.si.edu/",
)
VENTS = (ERTA_ALE, KILAUEA)


def counts(celsius: float) -> int:
    return int(round((celsius + KELVIN_ZERO_C) / 0.02))


def granule_of(points: list[tuple[float, float, float]]):
    """A synthetic one-row granule from (lat, lon, celsius) triples."""
    raw = np.array([[counts(celsius) for _, _, celsius in points]], dtype=np.uint16)
    qc = np.zeros((1, len(points)), dtype=np.uint8)
    lat_sub = np.array([[lat for lat, _, _ in points]])
    lon_sub = np.array([[lon for _, lon, _ in points]])
    return raw, qc, lat_sub, lon_sub


def field_of(points, fire_exclusion=None, sources=VENTS):
    raw, qc, lat_sub, lon_sub = granule_of(points)
    return prepare_granule(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        fire_exclusion=fire_exclusion,
        volcanic_sources=sources,
    )


def anomalies_of(points, fire_exclusion=None, sources=VENTS):
    field = field_of(points, fire_exclusion=fire_exclusion, sources=sources)
    found = granule_anomalies(
        field, observed_at=OBSERVED_AT, granule_id=GRANULE, volcanic_sources=sources
    )
    return field, found


def offset_north(source: VolcanicSource, km: float) -> tuple[float, float]:
    """A coordinate exactly ``km`` due north of a vent."""
    return (source.lat + km / KM_PER_DEGREE, source.lon)


def reading(max_c: float, product: str = TERRA, tile=(12, 29)) -> TileMax:
    tile_lat, tile_lon = tile
    return TileMax(
        tile_lat=tile_lat,
        tile_lon=tile_lon,
        max_c=max_c,
        max_lat=tile_lat + 0.5,
        max_lon=tile_lon + 0.5,
        observed_at=OBSERVED_AT,
        granule_id=f"{product}.A2014140.0920.061.NRT.hdf",
    )


# --- vent radius matching -------------------------------------------------------------


def test_a_pixel_on_the_vent_is_volcanic():
    inside = volcanic_vent_indices(
        np.array([ERTA_ALE.lat]), np.array([ERTA_ALE.lon]), VENTS
    )
    assert inside.tolist() == [0]


def test_a_pixel_beyond_the_radius_is_not():
    lat, lon = offset_north(ERTA_ALE, ERTA_ALE.radius_km + 1.0)
    assert volcanic_vent_indices(np.array([lat]), np.array([lon]), VENTS).tolist() == [-1]


def test_the_radius_itself_is_inside():
    """The radius is the edge of the vent's heat, not the first point outside it.

    A vent at null island with a radius of exactly one degree of latitude, so
    the boundary lands on a distance floating point can represent exactly and
    the test is about the comparison rather than about rounding.
    """
    vent = VolcanicSource(
        slug="unit", name="Unit", country="X", lat=0.0, lon=0.0, radius_km=KM_PER_DEGREE
    )
    lat = np.array([1.0, 1.000001])
    lon = np.array([0.0, 0.0])
    assert volcanic_vent_indices(lat, lon, (vent,)).tolist() == [0, -1]


def test_two_overlapping_vents_pick_the_nearer_one():
    near = VolcanicSource(slug="near", name="Near", country="X", lat=0.0, lon=0.0, radius_km=50.0)
    far = VolcanicSource(slug="far", name="Far", country="X", lat=0.3, lon=0.0, radius_km=50.0)

    # 0.05 degrees north of the first vent: inside both radii, nearer the first.
    vent = nearest_vent(0.05, 0.0, (near, far))
    assert vent is not None and vent.slug == "near"

    # And the order of the list does not decide it.
    assert nearest_vent(0.05, 0.0, (far, near)).slug == "near"


def test_an_empty_vent_list_matches_nothing():
    lat = np.array([ERTA_ALE.lat])
    assert volcanic_excluded_mask(lat, np.array([ERTA_ALE.lon]), ()).tolist() == [False]
    assert nearest_vent(ERTA_ALE.lat, ERTA_ALE.lon, ()) is None


def test_a_vent_beside_the_antimeridian_covers_both_sides_of_it():
    # Longitude differences wrap, or half of such a vent's heat would be missed.
    vent = VolcanicSource(
        slug="edge", name="Edge", country="X", lat=0.0, lon=179.99, radius_km=7.0
    )
    lat = np.array([0.0, 0.0])
    lon = np.array([-179.99, 179.99])
    assert volcanic_excluded_mask(lat, lon, (vent,)).tolist() == [True, True]


# --- the bounding-box prefilter --------------------------------------------------------
#
# The distance test allocates a granule-sized temporary per vent, so vents
# nowhere near the swath are skipped before it runs. The prefilter is an
# optimisation and must never change an answer.


def test_a_matched_vent_keeps_its_place_in_the_original_list():
    """The prefilter must not renumber the vents it skips past.

    Erta Ale is second in this list and the only candidate for these
    coordinates. If the returned index pointed into the filtered list instead
    of the given one, every row would cite the wrong volcano.
    """
    sources = (KILAUEA, ERTA_ALE)
    indices = volcanic_vent_indices(np.array([13.59]), np.array([40.67]), sources)

    assert indices.tolist() == [1]
    assert nearest_vent(13.59, 40.67, sources).slug == "erta-ale"


def test_vents_nowhere_near_the_swath_are_skipped():
    sahara = (np.array([[15.0, 25.0]]), np.array([[0.0, 20.0]]))
    assert vents_in_range(*sahara, VENTS) == ()

    ethiopia = (np.array([[10.0, 20.0]]), np.array([[35.0, 45.0]]))
    assert [source.slug for source in vents_in_range(*ethiopia, VENTS)] == ["erta-ale"]


def test_a_vent_just_outside_the_coordinates_is_still_a_candidate():
    # Its radius reaches into them, which is what the margin is for.
    lat, lon = offset_north(ERTA_ALE, ERTA_ALE.radius_km / 2)
    assert [source.slug for source in vents_in_range(
        np.array([[lat]]), np.array([[lon]]), VENTS
    )] == ["erta-ale"]


def test_a_vent_across_the_antimeridian_is_still_a_candidate():
    """The prefilter measures longitude the way the distance test does.

    A vent at 179.99 E is two kilometres from a swath edge at 179.99 W. Compared
    without wrapping, the two look 360 degrees apart and the vent would be
    skipped -- silently letting its heat into the weather archive.
    """
    vent = VolcanicSource(
        slug="edge", name="Edge", country="X", lat=0.0, lon=179.99, radius_km=7.0
    )
    swath_lat = np.array([[0.0, 1.0]])
    swath_lon = np.array([[-179.99, -179.0]])

    assert [source.slug for source in vents_in_range(swath_lat, swath_lon, (vent,))] == ["edge"]
    assert volcanic_excluded_mask(swath_lat, swath_lon, (vent,)).tolist() == [[True, False]]


def test_a_swath_that_crosses_the_antimeridian_excludes_no_vent_early():
    # Its longitude range spans the globe, so the prefilter defers to the
    # distance test rather than guessing which side the swath is on.
    # Latitudes wide enough to cover both vents, so longitude is the only
    # question this asks.
    swath_lat = np.array([[-60.0, 60.0]])
    swath_lon = np.array([[-179.9, 179.9]])
    assert len(vents_in_range(swath_lat, swath_lon, VENTS)) == len(VENTS)


def test_coordinates_entirely_off_the_globe_match_nothing():
    # MODIS uses -999.0 for unlocated pixels; they must not drag the bounding
    # box across the planet.
    off = np.full((2, 2), -999.0)
    assert vents_in_range(off, off, VENTS) == ()
    assert volcanic_vent_indices(off, off, VENTS).tolist() == [[-1, -1], [-1, -1]]


# --- the bundled list ------------------------------------------------------------------


def test_the_bundled_list_classifies_the_reading_that_motivated_the_screen():
    """The archive's top entry was Erta Ale's lava lake at 90.37 C.

    Asserted by coordinate rather than by slug, so a curated list is free to
    name and cite the vent however it likes as long as it still covers the
    ground that produced that reading.
    """
    sources = load_volcanic_sources()
    assert sources, "the bundled volcanic source list is empty"
    assert nearest_vent(13.59, 40.67, sources) is not None


def test_a_missing_list_is_survivable_and_empty(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert load_volcanic_sources(tmp_path / "not-here.json") == ()
    assert "could not be read" in caplog.text


def test_a_malformed_entry_is_skipped_rather_than_taking_the_list_down():
    sources = parse_volcanic_sources(
        [
            {"slug": "good", "name": "Good", "country": "X", "lat": 1.0, "lon": 2.0},
            {"slug": "no-coordinates", "name": "Bad", "country": "X"},
        ]
    )
    assert [source.slug for source in sources] == ["good"]


# --- volcanic pixels leave the weather path -------------------------------------------


def test_a_lava_lake_never_becomes_a_weather_reading():
    """The whole point: 90.37 C on the vent must not reach the weather outputs.

    Tile maxima, raster pixels and the all-time archive all read the one
    ``keep`` mask, so excluding the pixel there excludes it from all three.
    """
    lake = (13.59, 40.67, 90.37)
    desert = (13.59, 40.20, 55.0)  # same 1-degree tile, 51 km from the vent
    field = field_of([lake, desert])

    assert field.keep.tolist() == [[False, True]]
    assert field.volcanic_tiles == frozenset({(13, 40)})

    tiles = tile_maxima(
        field.celsius,
        field.keep,
        field.lat,
        field.lon,
        observed_at=OBSERVED_AT,
        granule_id=GRANULE,
        volcanic_tiles=field.volcanic_tiles,
    )
    assert tiles[(13, 40)].max_c == pytest.approx(55.0, abs=0.02)
    assert tiles[(13, 40)].qc_note.endswith(VOLCANIC_MASKED_NOTE)

    # The raster paints from the same mask, so the pyramid loses it too.
    store: dict[tuple[int, int], np.ndarray] = {}
    raster.accumulate_granule(
        store, field.celsius, field.lat, field.lon, valid=field.keep
    )
    hottest = max(
        int(value) for tile in store.values() for value in tile[tile != raster.EMPTY_CENTI_C]
    )
    assert hottest == pytest.approx(5500, abs=2)


def test_the_lava_lake_becomes_an_anomaly_naming_its_vent():
    _, found = anomalies_of([(13.59, 40.67, 90.37)])

    assert list(found) == [(13, 40, CAUSE_VOLCANIC)]
    anomaly = found[(13, 40, CAUSE_VOLCANIC)]
    assert anomaly.tile.max_c == pytest.approx(90.37, abs=0.02)
    assert anomaly.source_slug == "erta-ale"
    assert anomaly.tile.qc_note.endswith(VOLCANIC_ANOMALY_NOTE)


def test_warm_ground_on_a_volcano_is_not_published_as_an_anomaly():
    # Every pixel near a vent is technically volcanic; most are just warm.
    # Half a degree either side of the floor: the granule's stored counts are
    # quantised to 0.02 K, so a value sitting exactly on it is not a clean test.
    _, cool = anomalies_of([(13.59, 40.67, VOLCANIC_ANOMALY_MIN_C - 0.5)])
    _, hot = anomalies_of([(13.59, 40.67, VOLCANIC_ANOMALY_MIN_C + 0.5)])

    assert cool == {}
    assert list(hot) == [(13, 40, CAUSE_VOLCANIC)]


# --- volcanic wins over fire ----------------------------------------------------------


def test_a_lava_lake_that_also_trips_the_fire_mask_is_classified_volcanic():
    """MOD14 flags a lava lake as readily as a wildfire. The vent list decides.

    Both screens would claim this pixel. Volcanic runs first, so the row names
    a citable vent instead of filing the same heat as an anonymous fire.
    """
    lake = (13.59, 40.67, 90.37)
    keys = fire_exclusion_keys(np.array([13.59]), np.array([40.67]))
    field, found = anomalies_of([lake], fire_exclusion=keys)

    assert list(found) == [(13, 40, CAUSE_VOLCANIC)]
    assert found[(13, 40, CAUSE_VOLCANIC)].source_slug == "erta-ale"

    # The fire mask never saw the pixel: it was gone before that screen ran.
    assert field.fire_pixels is None
    assert field.fire_tiles == frozenset()


def test_the_same_fire_away_from_any_vent_is_a_wildfire():
    burning = (30.0, 20.0, 90.37)
    keys = fire_exclusion_keys(np.array([30.0]), np.array([20.0]))
    _, found = anomalies_of([burning], fire_exclusion=keys)

    assert list(found) == [(30, 20, CAUSE_WILDFIRE)]
    assert found[(30, 20, CAUSE_WILDFIRE)].source_slug is None


# --- notable wildfires ----------------------------------------------------------------


def test_a_hot_fire_is_captured_and_a_cooler_one_is_not():
    keys = fire_exclusion_keys(np.array([30.0]), np.array([20.0]))

    _, hot = anomalies_of([(30.0, 20.0, WILDFIRE_ANOMALY_MIN_C)], fire_exclusion=keys)
    _, cool = anomalies_of([(30.0, 20.0, WILDFIRE_ANOMALY_MIN_C - 0.5)], fire_exclusion=keys)

    assert list(hot) == [(30, 20, CAUSE_WILDFIRE)]
    assert hot[(30, 20, CAUSE_WILDFIRE)].tile.qc_note.endswith(ACTIVE_FIRE_ANOMALY_NOTE)
    # Below the bar it is still excluded from the weather path, just not notable.
    assert cool == {}


def test_a_wildfire_row_is_the_hottest_burnt_pixel_of_its_tile():
    # Two burning pixels in one 1-degree tile: one row, holding the hotter.
    keys = fire_exclusion_keys(np.array([30.0, 30.5]), np.array([20.0, 20.5]))
    _, found = anomalies_of(
        [(30.0, 20.0, 82.0), (30.5, 20.5, 95.0)], fire_exclusion=keys
    )

    assert list(found) == [(30, 20, CAUSE_WILDFIRE)]
    assert found[(30, 20, CAUSE_WILDFIRE)].tile.max_c == pytest.approx(95.0, abs=0.02)


def test_a_granule_with_nothing_excluded_produces_no_anomalies():
    field, found = anomalies_of([(30.0, 20.0, 55.0)])
    assert found == {}
    assert field.fire_pixels is None and field.volcanic_pixels is None


# --- failed corroboration --------------------------------------------------------------


def test_a_rejected_reading_is_also_published_as_an_anomaly():
    """The Sudan case: Terra 85.73 C, Aqua 57.77 C, 85 minutes apart.

    It was already barred from the archive. It is now also shown, in the
    section that says why it is not weather.
    """
    screen = corroborate_day(
        {
            TERRA: {(12, 29): reading(85.73, TERRA)},
            AQUA: {(12, 29): reading(57.77, AQUA)},
        }
    )

    assert [(a.cause, a.tile.max_c) for a in screen.anomalies] == [
        (CAUSE_FAILED_CORROBORATION, 85.73)
    ]
    # And the corroborated reading still stands as the tile's weather.
    assert screen.tiles[(12, 29)].max_c == 57.77


# --- uncorroborated readings -----------------------------------------------------------


def test_an_uncorroborated_record_never_reaches_the_all_time_archive():
    """Decision 2026-09-02, and the reason the whole change matters.

    The archive merges by maximum and a maximum is permanent, so a record no
    second satellite ever saw would sit there forever on one instrument's word.
    """
    day = cli.DayAccumulator()
    day.per_product = {TERRA: {(12, 29): reading(82.0, TERRA)}}

    screen = cli.screen_day(day, date(2026, 8, 30), None, dry_run=True)

    assert day.tiles == {}
    assert alltime.select_alltime_upserts({}, day.tiles) == []
    assert [a.cause for a in screen.anomalies] == [CAUSE_UNCORROBORATED]


def test_an_ordinary_lone_reading_is_untouched():
    # Below the record tier, a single satellite is all anyone ever expects.
    day = cli.DayAccumulator()
    day.per_product = {TERRA: {(12, 29): reading(CORROBORATION_THRESHOLD_C - 0.01, TERRA)}}

    screen = cli.screen_day(day, date(2026, 8, 30), None, dry_run=True)

    assert day.tiles[(12, 29)].qc_note == QC_NOTE
    assert screen.anomalies == []
    assert alltime.select_alltime_upserts({}, day.tiles) != []


def test_the_uncorroborated_tile_loses_its_record_tier_raster_pixels():
    """The map and the archive have to tell the same story.

    Record-tier pixels in the tile go with the reading. Ordinary heat in the
    same tile stays: nothing about it was ever in doubt.
    """
    day = cli.DayAccumulator()
    day.per_product = {TERRA: {(12, 29): reading(82.0, TERRA)}}
    day.raster = {}
    raster.accumulate_granule(
        day.raster,
        np.array([82.0, 55.0]),
        np.array([12.5, 12.6]),
        np.array([29.5, 29.6]),
    )

    cli.screen_day(day, date(2026, 8, 30), None, dry_run=True)

    survived = sorted(
        int(value)
        for tile in day.raster.values()
        for value in tile[tile != raster.EMPTY_CENTI_C]
    )
    assert survived == [5500]


def test_a_reading_exactly_on_the_threshold_loses_its_raster_pixel_too():
    # The raster stores hundredths and drops what is strictly above the ceiling,
    # so the ceiling sits one hundredth below the threshold to catch this.
    day = cli.DayAccumulator()
    day.per_product = {TERRA: {(12, 29): reading(CORROBORATION_THRESHOLD_C, TERRA)}}
    day.raster = {}
    raster.accumulate_granule(
        day.raster,
        np.array([CORROBORATION_THRESHOLD_C]),
        np.array([12.5]),
        np.array([29.5]),
    )

    cli.screen_day(day, date(2026, 8, 30), None, dry_run=True)

    assert not any(
        (tile != raster.EMPTY_CENTI_C).any() for tile in day.raster.values()
    )


# --- merging -------------------------------------------------------------------------


def test_one_row_per_tile_per_cause_holding_the_hottest():
    # A vent seen on three overpasses is one row, not three.
    accumulator: dict[tuple[int, int, str], Anomaly] = {}
    for max_c in (70.0, 90.37, 81.0):
        merge_anomalies(
            accumulator,
            {
                (13, 40, CAUSE_VOLCANIC): Anomaly(
                    tile=reading(max_c, TERRA, (13, 40)),
                    cause=CAUSE_VOLCANIC,
                    source_slug="erta-ale",
                )
            },
        )

    assert list(accumulator) == [(13, 40, CAUSE_VOLCANIC)]
    assert accumulator[(13, 40, CAUSE_VOLCANIC)].tile.max_c == 90.37


def test_two_causes_on_one_tile_are_two_rows():
    accumulator: dict[tuple[int, int, str], Anomaly] = {}
    merge_anomalies(
        accumulator,
        {
            (12, 29, CAUSE_FAILED_CORROBORATION): Anomaly(
                tile=reading(95.0), cause=CAUSE_FAILED_CORROBORATION
            ),
            (12, 29, CAUSE_UNCORROBORATED): Anomaly(
                tile=reading(80.0), cause=CAUSE_UNCORROBORATED
            ),
        },
    )
    assert len(accumulator) == 2


# --- the row payload -------------------------------------------------------------------


def test_the_anomaly_row_matches_the_table():
    anomaly = Anomaly(
        tile=TileMax(
            tile_lat=13,
            tile_lon=40,
            max_c=90.3749,
            max_lat=13.5912345,
            max_lon=40.6712345,
            observed_at=OBSERVED_AT,
            granule_id=GRANULE,
            qc_note=QC_NOTE + VOLCANIC_ANOMALY_NOTE,
        ),
        cause=CAUSE_VOLCANIC,
        source_slug="erta-ale",
    )

    row = build_anomaly_row(anomaly, date(2026, 8, 30), TERRA)

    assert row == {
        "reading_date": "2026-08-30",
        "satellite": "Terra",
        "product": TERRA,
        "tile_lat": 13,
        "tile_lon": 40,
        "max_c": 90.37,  # numeric(5,2) in the schema
        "max_lat": 13.591235,  # double precision in the schema, rounded to 6dp
        "max_lon": 40.671234,
        "observed_at": OBSERVED_AT,
        "granule_id": GRANULE,
        "qc_note": QC_NOTE + VOLCANIC_ANOMALY_NOTE,
        "cause": CAUSE_VOLCANIC,
        "source_slug": "erta-ale",
        # Volcanic rows are never reverse geocoded: the site names them, and
        # their country, from the cited vent list instead (decision 2026-09-02).
        "place_name": None,
        "country": None,
    }


def test_a_wildfire_row_carries_no_source_slug():
    # Naming a source would imply a citation the row does not have.
    row = build_anomaly_row(
        Anomaly(tile=reading(95.0, TERRA, (30, 20)), cause=CAUSE_WILDFIRE),
        date(2026, 8, 30),
        TERRA,
    )
    assert row["cause"] == CAUSE_WILDFIRE
    assert row["source_slug"] is None


# --- the write and dry-run paths -------------------------------------------------------


def volcanic_day() -> cli.DayAccumulator:
    day = cli.DayAccumulator()
    day.anomalies = {
        TERRA: {
            (13, 40, CAUSE_VOLCANIC): Anomaly(
                tile=reading(90.37, TERRA, (13, 40)),
                cause=CAUSE_VOLCANIC,
                source_slug="erta-ale",
            )
        }
    }
    return day


def test_a_dry_run_prints_the_rows_and_writes_nothing(capsys, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a dry run must not construct a writer")

    monkeypatch.setattr(cli, "SupabaseWriter", refuse)
    day = volcanic_day()

    cli.screen_day(day, date(2026, 8, 30), None, dry_run=True)

    output = capsys.readouterr().out
    assert "anomalies (dry run, nothing written)" in output
    assert "1 row(s) would be upserted into kiln.anomaly_readings" in output
    assert "90.37 C" in output
    assert CAUSE_VOLCANIC in output and "erta-ale" in output


def test_a_dry_run_with_nothing_to_report_says_so(capsys):
    cli.screen_day(cli.DayAccumulator(), date(2026, 8, 30), None, dry_run=True)
    assert "no non-weather readings" in capsys.readouterr().out


def test_the_rows_are_upserted_per_product(monkeypatch):
    written: list[tuple[str, str, list]] = []

    class RecordingWriter:
        def __init__(self, session, service_key, **kwargs):
            pass

        def upsert_readings(self, tiles, reading_date, product):
            return len(tiles)

        def upsert_anomalies(self, anomalies, reading_date, product, place_names=None):
            written.append((product, reading_date.isoformat(), list(anomalies)))
            return len(anomalies)

    monkeypatch.setattr(cli, "SupabaseWriter", RecordingWriter)

    day = volcanic_day()
    # Plus one the corroboration screen finds, on the other satellite.
    day.per_product = {AQUA: {(12, 29): reading(82.0, AQUA)}}

    cli.screen_day(day, date(2026, 8, 30), "service-key", dry_run=False)

    assert [product for product, _, _ in written] == [TERRA, AQUA]
    assert {a.cause for _, _, found in written for a in found} == {
        CAUSE_VOLCANIC,
        CAUSE_UNCORROBORATED,
    }
    assert all(when == "2026-08-30" for _, when, _ in written)


def test_the_two_sections_are_disjoint():
    """Nothing published as an anomaly may also be published as weather.

    Guaranteed by construction rather than by agreement: the anomalies come out
    of the pixels the screens removed from ``keep``, and the weather comes out
    of what is left.
    """
    lake = (13.59, 40.67, 90.37)
    desert = (13.59, 40.20, 55.0)
    keys = fire_exclusion_keys(np.array([30.0]), np.array([20.0]))
    field, found = anomalies_of([lake, desert, (30.0, 20.0, 88.0)], fire_exclusion=keys)

    weather = tile_maxima(
        field.celsius,
        field.keep,
        field.lat,
        field.lon,
        observed_at=OBSERVED_AT,
        granule_id=GRANULE,
    )

    assert sorted(cause for _, _, cause in found) == [CAUSE_VOLCANIC, CAUSE_WILDFIRE]
    assert {(13, 40, CAUSE_VOLCANIC), (30, 20, CAUSE_WILDFIRE)} == set(found)
    # The only weather left is the desert pixel; both excluded tiles kept none
    # of their excluded heat.
    assert list(weather) == [(13, 40)]
    assert weather[(13, 40)].max_c == pytest.approx(55.0, abs=0.02)
