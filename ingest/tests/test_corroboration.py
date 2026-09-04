"""Cross-satellite corroboration. Pure functions, no network.

Terra and Aqua cross the same ground about 90 minutes apart. Near local noon
the ground does not change much over that interval, so two very different
readings of the same tile on the same day are one temperature and one artifact.
The archive merges by maximum and never forgets, so the artifact has to be
caught before it gets there.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from kiln_ingest.raster import (
    accumulate_granule,
    EMPTY_CENTI_C,
    drop_above_ceilings,
    pixel_center_degrees,
    project_to_pixels,
    to_centi_celsius,
)
from kiln_ingest.science import (
    CORROBORATION_REJECTED_NOTE,
    CORROBORATION_THRESHOLD_C,
    CORROBORATION_TOLERANCE_K,
    QC_NOTE,
    UNCORROBORATED_NOTE,
    TileMax,
    corroborate_day,
)

TERRA = "MOD11_L2"
AQUA = "MYD11_L2"


def reading(max_c: float, product: str = TERRA, tile=(12, 29)) -> TileMax:
    tile_lat, tile_lon = tile
    return TileMax(
        tile_lat=tile_lat,
        tile_lon=tile_lon,
        max_c=max_c,
        max_lat=tile_lat + 0.5,
        max_lon=tile_lon + 0.5,
        observed_at="2014-05-20T09:20:00Z",
        granule_id=f"{product}.A2014140.0920.061.NRT.hdf",
    )


def day(terra: float | None = None, aqua: float | None = None, tile=(12, 29)):
    per_product = {}
    if terra is not None:
        per_product[TERRA] = {tile: reading(terra, TERRA, tile)}
    if aqua is not None:
        per_product[AQUA] = {tile: reading(aqua, AQUA, tile)}
    return per_product


# --- the case that motivated the screen ---------------------------------------------


def test_the_sudan_case_is_caught():
    """2014-05-20 tile (12,29): Terra 85.73 C, Aqua 57.77 C, 85 minutes apart.

    Ground cannot shed 28 K toward midday. The Terra value cleared QC and the
    fire mask, and without this screen it would have become a permanent record.
    """
    screen = corroborate_day(day(terra=85.73, aqua=57.77))

    # The archive gets the reading the other satellite supports, not the flare.
    assert screen.tiles[(12, 29)].max_c == 57.77
    assert screen.tiles[(12, 29)].qc_note == QC_NOTE

    # The flare is still a real observation, and says why it stops here.
    assert screen.rejected[(12, 29)].max_c == 85.73
    assert screen.rejected[(12, 29)].qc_note.endswith(CORROBORATION_REJECTED_NOTE)

    # And the raster may keep nothing hotter than the surviving reading.
    assert screen.ceilings == {(12, 29): 57.77}


# --- the four outcomes ---------------------------------------------------------------


def test_two_satellites_that_agree_are_both_believed():
    screen = corroborate_day(day(terra=80.0, aqua=79.0))

    assert screen.tiles[(12, 29)].max_c == 80.0
    assert screen.tiles[(12, 29)].qc_note == QC_NOTE
    assert screen.rejected == {}
    assert screen.ceilings == {}


def test_a_disagreement_rejects_the_higher_and_keeps_the_lower():
    screen = corroborate_day(day(terra=95.0, aqua=60.0))

    assert screen.tiles[(12, 29)].max_c == 60.0
    assert screen.rejected[(12, 29)].max_c == 95.0
    assert screen.ceilings[(12, 29)] == 60.0


def test_a_lone_reading_leaves_the_weather_archive():
    # Cloud over the other overpass, or an orbit gap. Decision 2026-09-02: a
    # record-tier reading nothing can check is not weather the archive will
    # claim. It used to be kept there with a caveat; it now goes to the
    # anomalies section instead, which loses none of it and overstates none of
    # it either.
    screen = corroborate_day(day(terra=82.0))

    assert (12, 29) not in screen.tiles
    assert screen.uncorroborated[(12, 29)].max_c == 82.0
    assert screen.uncorroborated[(12, 29)].qc_note.endswith(UNCORROBORATED_NOTE)
    assert screen.rejected == {}

    # And it is published as an anomaly, cause and all.
    assert [(a.cause, a.tile.max_c) for a in screen.anomalies] == [("uncorroborated", 82.0)]


def test_readings_below_the_threshold_are_left_completely_alone():
    screen = corroborate_day(day(terra=77.9, aqua=40.0))

    assert screen.tiles[(12, 29)].max_c == 77.9
    assert screen.tiles[(12, 29)].qc_note == QC_NOTE
    assert screen.rejected == {}
    assert screen.ceilings == {}


def test_a_lone_reading_below_the_threshold_is_not_marked():
    screen = corroborate_day(day(terra=60.0))
    assert screen.tiles[(12, 29)].qc_note == QC_NOTE


# --- the edges of the two constants --------------------------------------------------


def test_the_threshold_boundary():
    # Decision 2026-09-02: at the threshold a lone reading is routed out of the
    # weather archive; one hundredth below it is ordinary and untouched.
    at = corroborate_day(day(terra=CORROBORATION_THRESHOLD_C))
    below = corroborate_day(day(terra=CORROBORATION_THRESHOLD_C - 0.01))

    assert at.uncorroborated[(12, 29)].qc_note.endswith(UNCORROBORATED_NOTE)
    assert (12, 29) not in at.tiles
    assert below.tiles[(12, 29)].qc_note == QC_NOTE
    assert below.uncorroborated == {}


def test_the_tolerance_boundary():
    exact = corroborate_day(day(terra=90.0, aqua=90.0 - CORROBORATION_TOLERANCE_K))
    past = corroborate_day(day(terra=90.0, aqua=90.0 - CORROBORATION_TOLERANCE_K - 0.01))

    # Exactly at tolerance still counts as agreement.
    assert exact.rejected == {}
    assert exact.tiles[(12, 29)].max_c == 90.0
    assert past.rejected[(12, 29)].max_c == 90.0


# --- the survivor's own standing -----------------------------------------------------


def test_a_record_tier_survivor_loses_its_witness_and_follows_it_out():
    # Terra 95 is rejected by Aqua 80. Aqua is itself record-tier, and the only
    # reading that could have backed it up just failed. Decision 2026-09-02: it
    # is no longer kept in the archive with a caveat -- it goes to anomalies
    # alongside the reading that discredited it, and the tile keeps no weather.
    screen = corroborate_day(day(terra=95.0, aqua=80.0))

    assert (12, 29) not in screen.tiles
    survivor = screen.uncorroborated[(12, 29)]
    assert survivor.max_c == 80.0
    assert survivor.qc_note.endswith(UNCORROBORATED_NOTE)
    assert screen.rejected[(12, 29)].max_c == 95.0

    # Two rows for one tile: one per cause, which is what the unique constraint
    # on kiln.anomaly_readings allows for.
    assert sorted(a.cause for a in screen.anomalies) == [
        "failed corroboration",
        "uncorroborated",
    ]


def test_a_survivor_below_the_threshold_needs_no_caveat():
    screen = corroborate_day(day(terra=95.0, aqua=50.0))
    assert screen.tiles[(12, 29)].qc_note == QC_NOTE


def test_the_direction_of_the_disagreement_does_not_matter():
    # Whichever satellite read hotter is the one rejected.
    hot_terra = corroborate_day(day(terra=95.0, aqua=60.0))
    hot_aqua = corroborate_day(day(terra=60.0, aqua=95.0))

    assert hot_terra.rejected[(12, 29)].granule_id.startswith(TERRA)
    assert hot_aqua.rejected[(12, 29)].granule_id.startswith(AQUA)
    assert hot_terra.tiles[(12, 29)].max_c == hot_aqua.tiles[(12, 29)].max_c == 60.0


# --- single-product runs -------------------------------------------------------------


def test_a_single_product_run_routes_every_record_tier_tile_to_anomalies():
    # Nothing to corroborate against in-process -- a --product run, or a
    # pre-Aqua date. Decision 2026-09-02: that is not a reason to admit a record
    # nothing witnessed. Ordinary tiles in the same run are unaffected.
    screen = corroborate_day({TERRA: {(12, 29): reading(85.0), (1, 1): reading(50.0, tile=(1, 1))}})

    assert screen.uncorroborated[(12, 29)].qc_note.endswith(UNCORROBORATED_NOTE)
    assert (12, 29) not in screen.tiles
    assert screen.tiles[(1, 1)].qc_note == QC_NOTE
    assert screen.rejected == {}


def test_an_empty_day_screens_to_nothing():
    empty = corroborate_day({})
    assert empty.tiles == {} and empty.rejected == {} and empty.ceilings == {}
    assert corroborate_day({TERRA: {}}).tiles == {}


# --- tiles are screened independently ------------------------------------------------


def test_each_tile_is_judged_on_its_own_readings():
    per_product = {
        TERRA: {
            (12, 29): reading(85.73, TERRA, (12, 29)),  # contradicted
            (30, 50): reading(80.0, TERRA, (30, 50)),  # corroborated
            (31, 51): reading(82.0, TERRA, (31, 51)),  # alone
            (5, 5): reading(45.0, TERRA, (5, 5)),  # ordinary
        },
        AQUA: {
            (12, 29): reading(57.77, AQUA, (12, 29)),
            (30, 50): reading(79.0, AQUA, (30, 50)),
            (5, 5): reading(44.0, AQUA, (5, 5)),
        },
    }

    screen = corroborate_day(per_product)

    assert list(screen.rejected) == [(12, 29)]
    assert screen.tiles[(30, 50)].qc_note == QC_NOTE
    # Decision 2026-09-02: the tile nobody else saw is routed to anomalies
    # rather than kept in the archive with a caveat.
    assert list(screen.uncorroborated) == [(31, 51)]
    assert (31, 51) not in screen.tiles
    assert screen.tiles[(5, 5)].max_c == 45.0
    assert screen.tiles[(5, 5)].qc_note == QC_NOTE


def test_the_annotated_set_is_what_the_daily_table_has_to_be_told():
    per_product = {
        TERRA: {(12, 29): reading(85.73, TERRA, (12, 29)), (31, 51): reading(82.0, TERRA, (31, 51))},
        AQUA: {(12, 29): reading(57.77, AQUA, (12, 29))},
    }

    screen = corroborate_day(per_product)

    notes = sorted(tile.qc_note.rsplit(";", 1)[-1].strip() for tile in screen.annotated)
    assert notes == ["rejected by cross-satellite corroboration", "single-satellite, uncorroborated"]
    assert len(screen.annotated) == 2


def test_the_screen_does_not_mutate_what_it_was_given():
    per_product = day(terra=85.73, aqua=57.77)
    original = per_product[TERRA][(12, 29)]

    corroborate_day(per_product)

    assert per_product[TERRA][(12, 29)] is original
    assert original.qc_note == QC_NOTE


# --- the raster ceiling ---------------------------------------------------------------


def store_with(*points: tuple[float, float, float]):
    """A base-zoom store built the way the pipeline builds it, from (lat, lon, C)."""
    store: dict[tuple[int, int], np.ndarray] = {}
    accumulate_granule(
        store,
        np.array([celsius for _, _, celsius in points]),
        np.array([lat for lat, _, _ in points]),
        np.array([lon for _, lon, _ in points]),
    )
    return store


def observed(store) -> list[int]:
    """Every pixel in the store that holds a reading, hottest first."""
    values = [int(v) for tile in store.values() for v in tile[tile != EMPTY_CENTI_C]]
    return sorted(values, reverse=True)


def test_pixel_centres_invert_the_projection():
    lat, lon = pixel_center_degrees(64, 64)
    # Tile (64, 64) starts at null island and runs east and south from there.
    assert lon[0] == pytest.approx(0.0, abs=0.01)
    assert lat[0] == pytest.approx(0.0, abs=0.01)
    assert lon[-1] > lon[0]
    assert lat[-1] < lat[0]


def test_a_pixel_centre_round_trips_back_to_its_own_pixel():
    # The assignment of a pixel to a 1-degree tile has to agree with the
    # projection that put it there, or the ceiling would clip the wrong ground.
    lat, lon = pixel_center_degrees(64, 64)
    px, py = project_to_pixels(np.array([lat[100]]), np.array([lon[200]]))
    assert (int(px[0]), int(py[0])) == (64 * 256 + 200, 64 * 256 + 100)


def test_a_ceiling_drops_the_pixels_above_it():
    store = store_with((12.5, 29.5, 85.73), (12.6, 29.6, 50.0))
    assert observed(store) == [8573, 5000]

    dropped = drop_above_ceilings(store, {(12, 29): 57.77})

    assert dropped == 1
    assert observed(store) == [5000]


def test_a_ceiling_leaves_neighbouring_tiles_alone():
    # One degree east is tile (12, 30), which nothing contradicted.
    store = store_with((12.5, 29.5, 85.73), (12.5, 30.5, 85.73))
    assert observed(store) == [8573, 8573]

    dropped = drop_above_ceilings(store, {(12, 29): 57.77})

    assert dropped == 1
    assert observed(store) == [8573]


def test_no_ceilings_is_a_no_op():
    store = store_with((12.5, 29.5, 85.73))
    before = {key: tile.copy() for key, tile in store.items()}

    assert drop_above_ceilings(store, {}) == 0
    assert all(np.array_equal(store[key], tile) for key, tile in before.items())


def test_pixels_at_or_below_the_ceiling_survive():
    store = store_with((12.5, 29.5, 57.77))
    assert drop_above_ceilings(store, {(12, 29): 57.77}) == 0
    assert observed(store) == [int(to_centi_celsius(np.array([57.77]))[0])]


def test_a_dropped_pixel_becomes_unobserved_rather_than_clamped():
    # Clamping to the ceiling would publish a temperature no instrument
    # recorded. A gap is the honest answer.
    store = store_with((12.5, 29.5, 85.73))
    drop_above_ceilings(store, {(12, 29): 57.77})
    assert observed(store) == []


# --- the screen's place in the flow ---------------------------------------------------


def test_the_screen_governs_the_raster_and_the_archive_together():
    """screen_day is the seam: it must reach both stages, not just one.

    The archive is protected by ``day.tiles`` and the pyramid by the pixels in
    ``day.raster``. A screen that only reached one of them would publish a map
    showing a temperature the table refuses to record.
    """
    from kiln_ingest import cli

    day_state = cli.DayAccumulator()
    day_state.per_product = {
        TERRA: {(12, 29): reading(85.73, TERRA)},
        AQUA: {(12, 29): reading(57.77, AQUA)},
    }
    day_state.raster = store_with((12.5, 29.5, 85.73), (12.6, 29.6, 50.0))

    screen = cli.screen_day(day_state, date(2014, 5, 20), None, dry_run=True)

    # (a) the archive sees the corroborated reading
    assert day_state.tiles[(12, 29)].max_c == 57.77
    # (b) the pyramid has lost the pixels nothing supports
    assert observed(day_state.raster) == [5000]
    assert list(screen.rejected) == [(12, 29)]


def test_a_dry_run_reports_the_screen_and_writes_no_rows(capsys):
    from kiln_ingest import cli

    day_state = cli.DayAccumulator()
    day_state.per_product = {
        TERRA: {(12, 29): reading(85.73, TERRA), (31, 51): reading(82.0, TERRA, (31, 51))},
        AQUA: {(12, 29): reading(57.77, AQUA)},
    }

    cli.screen_day(day_state, date(2014, 5, 20), None, dry_run=True)

    output = capsys.readouterr().out
    assert "cross-satellite corroboration" in output
    assert "1 rejected" in output
    # Decision 2026-09-02: no longer "kept but single-satellite".
    assert "1 single-satellite and uncorroborated, routed to anomalies" in output
    assert "85.73 C" in output and "57.77 C" in output

    # And the rows it would have written are printed rather than written.
    assert "anomalies (dry run, nothing written)" in output
    assert "failed corroboration" in output and "uncorroborated" in output


def test_the_daily_rows_are_rewritten_with_the_screen_s_verdict(monkeypatch):
    """The daily table is written before the other satellite is seen.

    These are real observations and they stay published; this pass only
    corrects what their note says about them.
    """
    from kiln_ingest import cli

    written: list[tuple[str, list]] = []

    class RecordingWriter:
        def __init__(self, session, service_key, **kwargs):
            pass

        def upsert_readings(self, tiles, reading_date, product):
            written.append((product, list(tiles)))
            return len(tiles)

    monkeypatch.setattr(cli, "SupabaseWriter", RecordingWriter)

    screen = corroborate_day({
        TERRA: {(12, 29): reading(85.73, TERRA), (31, 51): reading(82.0, TERRA, (31, 51))},
        AQUA: {(12, 29): reading(57.77, AQUA)},
    })
    count = cli.rewrite_screened_rows(None, screen, date(2014, 5, 20), "service-key")

    assert count == 2
    assert [product for product, _ in written] == [TERRA]
    notes = sorted(tile.qc_note.rsplit(";", 1)[-1].strip() for _, tiles in written for tile in tiles)
    assert notes == [
        "rejected by cross-satellite corroboration",
        "single-satellite, uncorroborated",
    ]


def test_a_clean_day_rewrites_nothing():
    from kiln_ingest import cli

    screen = corroborate_day(day(terra=50.0, aqua=49.0))
    assert cli.rewrite_screened_rows(None, screen, date(2014, 5, 20), "key") == 0
