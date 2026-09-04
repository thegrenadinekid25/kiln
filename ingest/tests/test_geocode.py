"""Reverse geocoding: cells, cache, naming policy, etiquette. No network.

Every network call in these tests goes to a fake session that records what it
was asked and answers from a script. Nominatim is a donated public service and
a test suite is not a good reason to spend its quota.
"""

from __future__ import annotations

from datetime import date

import pytest

from kiln_ingest import cli, geocode
from kiln_ingest.geocode import (
    CELL_DEGREES,
    MIN_REQUEST_INTERVAL_S,
    NOMINATIM_URL,
    NOMINATIM_ZOOM_FALLBACK,
    USER_AGENT,
    Place,
    PlaceNameResolver,
    anomaly_places,
    backfill,
    backfill_cells,
    cell_bounds,
    cell_key,
    country_name,
    display_name,
    place_for,
    tile_places,
)
from kiln_ingest.science import (
    CAUSE_UNCORROBORATED,
    CAUSE_VOLCANIC,
    CAUSE_WILDFIRE,
    Anomaly,
    TileMax,
)
from kiln_ingest.supabase_io import (
    SupabaseWriter,
    build_alltime_row,
    build_anomaly_rows,
    build_reading_rows,
)

TERRA = "MOD11_L2"
OBSERVED_AT = "2026-08-30T08:20:00Z"


# --- doubles --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Answers reverse lookups from a per-cell script, recording every call.

    ``answers`` keys are usually a plain ``(lat, lon)`` pair, answered the same
    at any zoom. A ``(lat, lon, zoom)`` triple answers one zoom level only,
    which is what lets a test give the fine-zoom and fallback-zoom attempts for
    the same cell different responses.
    """

    def __init__(self, answers=None, default=None):
        self.answers = answers or {}
        self.default = default if default is not None else {"error": "Unable to geocode"}
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout}
        )
        lat, lon = float(params["lat"]), float(params["lon"])
        zoom = params.get("zoom")
        if (lat, lon, zoom) in self.answers:
            answer = self.answers[(lat, lon, zoom)]
        else:
            answer = self.answers.get((lat, lon), self.default)
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(answer)

    @property
    def cells_asked(self) -> list[tuple[float, float]]:
        return [(float(c["params"]["lat"]), float(c["params"]["lon"])) for c in self.calls]


class FakeWriter:
    """A stand-in for the PostgREST client, holding the cache in a dict."""

    def __init__(self, cached=None, unnamed=None):
        self.cached = dict(cached or {})
        self.unnamed = unnamed or {}
        self.upserted: list[dict] = []
        self.patches: list[tuple] = []
        self.fetches: list[list] = []

    def fetch_place_names(self, cells):
        self.fetches.append(list(cells))
        return {cell: self.cached[cell] for cell in cells if cell in self.cached}

    def upsert_place_names(self, rows):
        self.upserted.extend(rows)
        for row in rows:
            self.cached[(row["cell_lat"], row["cell_lon"])] = (
                row["place_name"],
                row["country"],
            )
        return len(rows)

    def fetch_unnamed_rows(self, table, select, extra_filter=""):
        return list(self.unnamed.get(table, []))

    def patch_place_name_in_cell(
        self, table, bounds, place_name, country=None, extra_filter=""
    ):
        self.patches.append((table, bounds, place_name, country, extra_filter))


class Clock:
    def __init__(self):
        self.slept: list[float] = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def resolver_with(session=None, writer=None, **kwargs) -> PlaceNameResolver:
    clock = Clock()
    resolver = PlaceNameResolver(
        session=session or FakeSession(), writer=writer, sleep=clock, **kwargs
    )
    resolver.clock = clock  # type: ignore[attr-defined]
    return resolver


def reading(max_c=90.0, lat=13.59, lon=40.67, tile=(13, 40)) -> TileMax:
    return TileMax(
        tile_lat=tile[0],
        tile_lon=tile[1],
        max_c=max_c,
        max_lat=lat,
        max_lon=lon,
        observed_at=OBSERVED_AT,
        granule_id=f"{TERRA}.A2026242.0820.061.NRT.hdf",
    )


# --- cells ----------------------------------------------------------------------------


def test_a_coordinate_rounds_to_its_nearest_half_degree():
    assert cell_key(13.59, 40.67) == (13.5, 40.5)
    assert cell_key(13.76, 40.80) == (14.0, 41.0)
    assert cell_key(0.24, 0.26) == (0.0, 0.5)


def test_rounding_works_the_same_south_and_west():
    assert cell_key(-13.59, -40.67) == (-13.5, -40.5)
    assert cell_key(-13.76, -40.80) == (-14.0, -41.0)


def test_a_cell_never_keys_as_negative_zero():
    # -0.0 and 0.0 are the same cell, and two keys for one cell would be two
    # lookups and two cache rows.
    cell = cell_key(-0.1, -0.1)
    assert cell == (0.0, 0.0)
    assert str(cell[0]) == "0.0" and str(cell[1]) == "0.0"


def test_neighbouring_readings_share_one_cell():
    # The whole reason the cache is per cell: one lookup serves a neighbourhood.
    assert cell_key(13.51, 40.51) == cell_key(13.70, 40.70)


def test_cell_bounds_contain_exactly_what_rounds_into_the_cell():
    lat_min, lat_max, lon_min, lon_max = cell_bounds((13.5, 40.5))
    assert (lat_min, lat_max) == (13.25, 13.75)
    assert (lon_min, lon_max) == (40.25, 40.75)

    # The bounds are the inverse of the rounding, which is what lets the
    # backfill name a whole cell with one range update.
    assert cell_key(lat_min, lon_min) == (13.5, 40.5)
    assert cell_key(lat_max - 0.001, lon_max - 0.001) == (13.5, 40.5)
    assert cell_key(lat_max, lon_max) != (13.5, 40.5)


def test_the_cell_size_is_half_a_degree():
    assert CELL_DEGREES == 0.5


def test_every_coordinate_falls_inside_the_bounds_of_its_own_cell():
    """The invariant the backfill's range updates rest on.

    A cell's rows are named by patching everything inside :func:`cell_bounds`,
    while the geocode itself is done at the coordinate the cell key came from.
    If a coordinate could round to one cell while sitting inside another's
    bounds, a reading would be given a neighbouring region's name. Boundaries
    are the interesting part: exact quarter-degrees are where a round-half-to-
    even would disagree with the half-open interval.
    """
    coordinates = [
        (lat / 100.0, lon / 100.0)
        for lat in range(-9000, 9001, 25)
        for lon in range(-18000, 18001, 725)
    ]

    for lat, lon in coordinates:
        cell = cell_key(lat, lon)
        lat_min, lat_max, lon_min, lon_max = cell_bounds(cell)
        assert lat_min <= lat < lat_max, f"{lat} is outside cell {cell}"
        assert lon_min <= lon < lon_max, f"{lon} is outside cell {cell}"


# --- the naming policy ------------------------------------------------------------------


def test_a_province_and_its_country_are_preferred():
    payload = {"address": {"state": "Eastern Province", "country": "Saudi Arabia"}}
    assert display_name(payload) == "Eastern Province, Saudi Arabia"


def test_a_county_stands_in_when_there_is_no_province():
    payload = {"address": {"county": "Inyo County", "country": "United States"}}
    assert display_name(payload) == "Inyo County, United States"


def test_a_province_outranks_a_county():
    payload = {
        "address": {
            "county": "Al-Ahsa",
            "state": "Eastern Province",
            "country": "Saudi Arabia",
        }
    }
    assert display_name(payload) == "Eastern Province, Saudi Arabia"


def test_the_country_alone_is_better_than_nothing():
    assert display_name({"address": {"country": "Chad"}}) == "Chad"


def test_other_province_keys_are_understood():
    # Nominatim fills different keys in different countries.
    assert display_name({"address": {"region": "Afar", "country": "Ethiopia"}}) == (
        "Afar, Ethiopia"
    )
    assert display_name({"address": {"province": "Kerman", "country": "Iran"}}) == (
        "Kerman, Iran"
    )


def test_a_city_state_is_not_named_twice():
    payload = {"address": {"state": "Singapore", "country": "Singapore"}}
    assert display_name(payload) == "Singapore"


def test_the_middle_of_an_ocean_has_no_name():
    assert display_name({"error": "Unable to geocode"}) is None


def test_a_response_with_no_address_has_no_name():
    assert display_name({"licence": "ODbL"}) is None
    assert display_name({"address": "not an object"}) is None
    assert display_name(None) is None
    assert display_name([]) is None


def test_blank_components_are_ignored():
    assert display_name({"address": {"state": "   ", "country": "Mali"}}) == "Mali"


# --- the country ---------------------------------------------------------------------
#
# The leaderboard filters by country, so it is captured as its own field rather
# than recovered from the display name: splitting "Al Karak, Jordan" on a comma
# is a guess, and address.country is an answer.


def test_the_country_comes_from_its_own_field():
    payload = {"address": {"state": "Eastern Province", "country": "Saudi Arabia"}}
    assert country_name(payload) == "Saudi Arabia"


def test_a_response_with_no_country_has_none():
    # Nominatim sometimes omits it, notably at sea borders.
    assert country_name({"address": {"state": "Baja California"}}) is None


def test_an_unusable_response_has_no_country():
    assert country_name({"error": "Unable to geocode"}) is None
    assert country_name({"licence": "ODbL"}) is None
    assert country_name(None) is None


def test_a_place_carries_the_name_and_the_country():
    payload = {"address": {"state": "Eastern Province", "country": "Saudi Arabia"}}
    assert place_for(payload) == Place("Eastern Province, Saudi Arabia", "Saudi Arabia")


def test_a_named_place_can_still_have_no_country():
    place = place_for({"address": {"state": "Baja California"}})
    assert place == Place("Baja California", None)


def test_a_country_only_answer_names_and_files_it_the_same():
    assert place_for({"address": {"country": "Chad"}}) == Place("Chad", "Chad")


def test_an_empty_place_is_what_the_ocean_gives():
    assert place_for({"error": "Unable to geocode"}) == Place(None, None)
    assert Place() == (None, None)


def test_a_place_is_a_plain_pair():
    # The row builders unpack it positionally, so the writer never imports the
    # geocoder. Order matters: name first, country second.
    assert tuple(Place("Al Karak, Jordan", "Jordan")) == ("Al Karak, Jordan", "Jordan")


# --- cache first ------------------------------------------------------------------------


def test_a_cached_cell_is_never_asked_about():
    writer = FakeWriter(cached={(13.5, 40.5): ("Afar, Ethiopia", "Ethiopia")})
    session = FakeSession()
    resolver = resolver_with(session=session, writer=writer)

    assert resolver.resolve([(13.5, 40.5)]) == {
        (13.5, 40.5): Place("Afar, Ethiopia", "Ethiopia")
    }
    assert session.calls == []
    assert writer.upserted == []


def test_a_cached_null_is_not_asked_about_again():
    """The reason nulls are cached at all.

    Ocean and empty desert are most of the globe. Without storing the null,
    every one of those cells would be asked about again every single day.
    """
    writer = FakeWriter(cached={(0.0, -30.0): (None, None)})
    session = FakeSession()
    resolver = resolver_with(session=session, writer=writer)

    assert resolver.resolve([(0.0, -30.0)]) == {(0.0, -30.0): Place(None, None)}
    assert session.calls == []


def test_only_the_misses_reach_the_network():
    writer = FakeWriter(cached={(13.5, 40.5): ("Afar, Ethiopia", "Ethiopia")})
    session = FakeSession(
        answers={(31.5, 35.5): {"address": {"state": "Al Karak", "country": "Jordan"}}}
    )
    resolver = resolver_with(session=session, writer=writer)

    names = resolver.resolve([(13.5, 40.5), (31.5, 35.5)])

    assert names == {
        (13.5, 40.5): Place("Afar, Ethiopia", "Ethiopia"),
        (31.5, 35.5): Place("Al Karak, Jordan", "Jordan"),
    }
    assert session.cells_asked == [(31.5, 35.5)]


def test_a_new_name_is_written_back_to_the_cache():
    writer = FakeWriter()
    session = FakeSession(
        answers={(13.5, 40.5): {"address": {"region": "Afar", "country": "Ethiopia"}}}
    )
    resolver = resolver_with(session=session, writer=writer)

    resolver.resolve([(13.5, 40.5)])

    assert len(writer.upserted) == 1
    row = writer.upserted[0]
    assert row["cell_lat"] == 13.5 and row["cell_lon"] == 40.5
    assert row["place_name"] == "Afar, Ethiopia"
    assert row["country"] == "Ethiopia"
    assert row["source"] == "nominatim"
    assert row["resolved_at"].endswith("+00:00")


def test_the_country_round_trips_through_the_cache():
    """A second run reads back both halves, not just the name.

    The cache is the only thing standing between the leaderboard and a fresh
    reverse geocode of every cell, so a country that did not survive storage
    would be a country the filter never sees.
    """
    writer = FakeWriter()
    session = FakeSession(
        answers={(13.5, 40.5): {"address": {"region": "Afar", "country": "Ethiopia"}}}
    )
    resolver_with(session=session, writer=writer).resolve([(13.5, 40.5)])

    # A new resolver over the same cache: no network, both halves intact.
    later_session = FakeSession()
    later = resolver_with(session=later_session, writer=writer)

    assert later.resolve([(13.5, 40.5)]) == {
        (13.5, 40.5): Place("Afar, Ethiopia", "Ethiopia")
    }
    assert later_session.calls == []


def test_a_cached_name_with_no_country_round_trips_as_such():
    writer = FakeWriter()
    session = FakeSession(answers={(31.5, -117.5): {"address": {"state": "Baja California"}}})
    resolver_with(session=session, writer=writer).resolve([(31.5, -117.5)])

    assert writer.upserted[0]["place_name"] == "Baja California"
    assert writer.upserted[0]["country"] is None

    later = resolver_with(session=FakeSession(), writer=writer)
    assert later.resolve([(31.5, -117.5)]) == {
        (31.5, -117.5): Place("Baja California", None)
    }


def test_a_cell_resolved_once_is_remembered_for_the_rest_of_the_run():
    session = FakeSession(
        answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}}
    )
    writer = FakeWriter()
    resolver = resolver_with(session=session, writer=writer)

    resolver.resolve([(13.5, 40.5)])
    resolver.resolve([(13.5, 40.5)])

    assert len(session.calls) == 1
    assert writer.fetches == [[(13.5, 40.5)]]


def test_an_unreadable_cache_does_not_stop_the_run():
    class BrokenWriter(FakeWriter):
        def fetch_place_names(self, cells):
            raise RuntimeError("PostgREST is down")

    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session, writer=BrokenWriter())

    assert resolver.resolve([(13.5, 40.5)]) == {
        (13.5, 40.5): Place("Ethiopia", "Ethiopia")
    }


def test_a_failed_cache_write_still_returns_the_names():
    class BrokenWriter(FakeWriter):
        def upsert_place_names(self, rows):
            raise RuntimeError("PostgREST is down")

    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session, writer=BrokenWriter())

    assert resolver.resolve([(13.5, 40.5)]) == {
        (13.5, 40.5): Place("Ethiopia", "Ethiopia")
    }


def test_the_resolver_works_with_no_cache_at_all():
    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session, writer=None)
    assert resolver.resolve([(13.5, 40.5)]) == {
        (13.5, 40.5): Place("Ethiopia", "Ethiopia")
    }


# --- etiquette ---------------------------------------------------------------------------


def test_calls_are_spaced_a_second_apart():
    """Nominatim's usage policy allows one request a second. This enforces it."""
    # Named at the fine zoom so none of the three cells triggers a fallback
    # retry: this test is about pacing between cells, not the retry itself.
    session = FakeSession(
        answers={
            (1.0, 1.0): {"address": {"country": "A"}},
            (2.0, 2.0): {"address": {"country": "B"}},
            (3.0, 3.0): {"address": {"country": "C"}},
        }
    )
    resolver = resolver_with(session=session)

    resolver.resolve([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)])

    assert len(session.calls) == 3
    # Between the calls, not before the first: three requests, two waits.
    assert resolver.clock.slept == [MIN_REQUEST_INTERVAL_S, MIN_REQUEST_INTERVAL_S]


def test_the_pacing_persists_across_separate_batches():
    session = FakeSession(
        answers={
            (1.0, 1.0): {"address": {"country": "A"}},
            (2.0, 2.0): {"address": {"country": "B"}},
        }
    )
    resolver = resolver_with(session=session)

    resolver.resolve([(1.0, 1.0)])
    resolver.resolve([(2.0, 2.0)])

    assert resolver.clock.slept == [MIN_REQUEST_INTERVAL_S]


def test_the_fallback_retry_also_respects_the_rate_limit():
    """The retry is a second real request and must wait its turn too."""
    session = FakeSession(default={"error": "Unable to geocode"})
    resolver = resolver_with(session=session)

    resolver.resolve([(1.0, 1.0)])

    assert len(session.calls) == 2
    assert session.calls[0]["params"]["zoom"] == 8
    assert session.calls[1]["params"]["zoom"] == NOMINATIM_ZOOM_FALLBACK
    # One wait between the fine-zoom attempt and the fallback, none before the
    # very first request this resolver ever made.
    assert resolver.clock.slept == [MIN_REQUEST_INTERVAL_S]


def test_every_request_identifies_the_product():
    # A generic User-Agent gets the whole service blocked for everybody.
    session = FakeSession()
    resolver_with(session=session).resolve([(1.0, 1.0)])

    call = session.calls[0]
    assert call["url"] == NOMINATIM_URL
    assert call["headers"]["User-Agent"] == USER_AGENT
    assert "thehottestplaceintheworld.bytortoise.com" in call["headers"]["User-Agent"]
    assert call["params"]["format"] == "jsonv2"
    assert call["params"]["zoom"] == 8
    assert call["params"]["accept-language"] == "en"
    assert call["timeout"]


# --- failure is not knowledge -------------------------------------------------------------


def test_a_result_with_no_name_is_cached_as_a_null():
    """Empty at both the fine and the fallback zoom is still a real answer."""
    writer = FakeWriter()
    session = FakeSession(default={"error": "Unable to geocode"})
    resolver = resolver_with(session=session, writer=writer)

    assert resolver.resolve([(0.0, -30.0)]) == {(0.0, -30.0): Place(None, None)}
    assert writer.upserted[0]["place_name"] is None
    assert writer.upserted[0]["country"] is None
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        FakeResponse(status_code=429),
        FakeResponse(status_code=503),
        FakeResponse(status_code=403),
        FakeResponse(payload=ValueError("not json")),
    ],
)
def test_a_failed_lookup_is_never_cached(failure):
    """A rate limit is not a fact about a place.

    Caching a null here would be permanent: the cell would never be asked about
    again, and an hour of Nominatim trouble would leave a scar on the map that
    no later run repairs. Unnamed today, asked again tomorrow.
    """
    writer = FakeWriter()
    session = FakeSession(default=failure)
    resolver = resolver_with(session=session, writer=writer)

    assert resolver.resolve([(13.5, 40.5)]) == {(13.5, 40.5): Place(None, None)}
    assert writer.upserted == []


def test_a_transport_failure_is_survived_per_cell():
    class ExplodingSession(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            if float(params["lat"]) == 13.5:
                raise ConnectionError("network is down")
            return super().get(url, params=params, headers=headers, timeout=timeout)

    writer = FakeWriter()
    session = ExplodingSession(
        answers={(31.5, 35.5): {"address": {"country": "Jordan"}}}
    )
    resolver = resolver_with(session=session, writer=writer)

    names = resolver.resolve([(13.5, 40.5), (31.5, 35.5)])

    # One cell fails, the other still gets its name.
    assert names == {
        (13.5, 40.5): Place(None, None),
        (31.5, 35.5): Place("Jordan", "Jordan"),
    }
    assert [row["place_name"] for row in writer.upserted] == ["Jordan"]


# --- wider-radius retry ------------------------------------------------------------------


def test_an_empty_fine_zoom_result_retries_once_at_the_fallback_zoom():
    writer = FakeWriter()
    session = FakeSession(
        answers={
            (0.0, -30.0, 8): {"error": "Unable to geocode"},
            (0.0, -30.0, NOMINATIM_ZOOM_FALLBACK): {"address": {"country": "Nowhere"}},
        }
    )
    resolver = resolver_with(session=session, writer=writer)

    names = resolver.resolve([(0.0, -30.0)])

    assert names == {(0.0, -30.0): Place("Nowhere", "Nowhere")}
    assert len(session.calls) == 2
    assert session.calls[0]["params"]["zoom"] == 8
    assert session.calls[1]["params"]["zoom"] == NOMINATIM_ZOOM_FALLBACK
    # Whichever attempt succeeds is what gets cached.
    assert writer.upserted[0]["place_name"] == "Nowhere"


def test_a_cell_named_at_the_fine_zoom_never_retries():
    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session)

    names = resolver.resolve([(13.5, 40.5)])

    assert names == {(13.5, 40.5): Place("Ethiopia", "Ethiopia")}
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["zoom"] == 8


def test_a_failure_on_the_fallback_attempt_is_not_cached_either():
    """The fine zoom coming back empty is knowledge; the fallback erroring is not."""
    writer = FakeWriter()
    session = FakeSession(
        answers={
            (0.0, -30.0, 8): {"error": "Unable to geocode"},
            (0.0, -30.0, NOMINATIM_ZOOM_FALLBACK): FakeResponse(status_code=503),
        }
    )
    resolver = resolver_with(session=session, writer=writer)

    assert resolver.resolve([(0.0, -30.0)]) == {(0.0, -30.0): Place(None, None)}
    assert writer.upserted == []


# --- dry runs -----------------------------------------------------------------------------


def test_a_dry_run_asks_nothing_and_records_what_it_would_have_asked():
    session = FakeSession()
    writer = FakeWriter(cached={(13.5, 40.5): ("Afar, Ethiopia", "Ethiopia")})
    resolver = resolver_with(session=session, writer=writer, dry_run=True)

    names = resolver.resolve([(13.5, 40.5), (31.5, 35.5)])

    assert session.calls == []
    assert writer.upserted == []
    # The cached one is still answered; only the miss is pending.
    assert names == {
        (13.5, 40.5): Place("Afar, Ethiopia", "Ethiopia"),
        (31.5, 35.5): Place(None, None),
    }
    assert resolver.pending == [(31.5, 35.5)]


def test_a_dry_run_lists_each_pending_cell_once():
    resolver = resolver_with(dry_run=True)
    resolver.resolve([(13.5, 40.5)])
    resolver.resolve([(13.5, 40.5), (31.5, 35.5)])
    assert resolver.pending == [(13.5, 40.5), (31.5, 35.5)]


def test_the_dry_run_report_names_the_cost(capsys):
    resolver = resolver_with(dry_run=True)
    resolver.resolve([(13.5, 40.5), (31.5, 35.5)])

    geocode.report_pending(resolver)

    output = capsys.readouterr().out
    assert "2 half-degree cell(s) would be reverse geocoded" in output
    assert "13.5" in output and "40.5" in output


def test_the_dry_run_report_says_when_the_cache_covers_everything(capsys):
    geocode.report_pending(resolver_with(dry_run=True))
    assert "already in the kiln.place_names cache" in capsys.readouterr().out


# --- wiring helpers -------------------------------------------------------------------------


def test_readings_are_keyed_back_to_their_tiles():
    session = FakeSession(
        answers={(13.5, 40.5): {"address": {"region": "Afar", "country": "Ethiopia"}}}
    )
    resolver = resolver_with(session=session)

    names = tile_places(resolver, [reading()])

    assert names == {(13, 40): Place("Afar, Ethiopia", "Ethiopia")}


def test_two_readings_in_one_cell_cost_one_request():
    session = FakeSession(
        answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}}
    )
    resolver = resolver_with(session=session)

    names = tile_places(
        resolver,
        [reading(lat=13.51, lon=40.51), reading(lat=13.60, lon=40.60, tile=(13, 40))],
    )

    assert len(session.calls) == 1
    assert names == {(13, 40): Place("Ethiopia", "Ethiopia")}


def test_volcanic_anomalies_are_never_geocoded():
    """A volcanic row already knows where it is, and better than this would.

    It carries the slug of a curated, cited vent; the site names it from that.
    Asking Nominatim would spend a request to learn something vaguer.
    """
    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session)

    lava = Anomaly(tile=reading(), cause=CAUSE_VOLCANIC, source_slug="erta-ale")
    fire = Anomaly(tile=reading(tile=(30, 20), lat=30.6, lon=20.6), cause=CAUSE_WILDFIRE)

    names = anomaly_places(resolver, [lava, fire])

    assert lava.key not in names
    assert fire.key in names
    # The fire's cell has no scripted answer, so it comes back empty at the
    # fine zoom and is retried once at the fallback zoom -- still never lava's
    # cell, which is the point of this test.
    assert session.cells_asked == [(30.5, 20.5), (30.5, 20.5)]


def test_an_uncorroborated_anomaly_is_geocoded():
    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session)

    unverified = Anomaly(tile=reading(), cause=CAUSE_UNCORROBORATED)
    names = anomaly_places(resolver, [unverified])

    assert names == {(13, 40, CAUSE_UNCORROBORATED): Place("Ethiopia", "Ethiopia")}


# --- the pipeline seams -----------------------------------------------------------------------


def test_the_daily_rows_carry_the_names_they_were_written_with(monkeypatch):
    written: list[tuple] = []

    class RecordingWriter:
        def __init__(self, session, service_key, **kwargs):
            pass

        def upsert_anomalies(self, anomalies, reading_date, product, places=None):
            written.append((product, places))
            return len(anomalies)

        def upsert_readings(self, tiles, reading_date, product, places=None):
            return len(tiles)

    monkeypatch.setattr(cli, "SupabaseWriter", RecordingWriter)

    session = FakeSession(answers={(13.5, 40.5): {"address": {"country": "Ethiopia"}}})
    resolver = resolver_with(session=session)

    day = cli.DayAccumulator()
    day.anomalies = {
        TERRA: {
            (13, 40, CAUSE_WILDFIRE): Anomaly(tile=reading(), cause=CAUSE_WILDFIRE)
        }
    }

    cli.screen_day(day, date(2026, 8, 30), "service-key", dry_run=False, resolver=resolver)

    assert written == [
        (TERRA, {(13, 40, CAUSE_WILDFIRE): Place("Ethiopia", "Ethiopia")})
    ]


def test_a_dry_run_of_the_whole_day_geocodes_nothing():
    session = FakeSession()
    resolver = resolver_with(session=session, dry_run=True)

    day = cli.DayAccumulator()
    day.anomalies = {
        TERRA: {
            (13, 40, CAUSE_WILDFIRE): Anomaly(tile=reading(), cause=CAUSE_WILDFIRE)
        }
    }

    cli.screen_day(day, date(2026, 8, 30), None, dry_run=True, resolver=resolver)

    assert session.calls == []
    assert resolver.pending == [(13.5, 40.5)]


# --- backfill ---------------------------------------------------------------------------------


def test_the_backfill_orders_cells_by_temperature():
    # An interrupted backfill should already have named what people look at.
    cells = backfill_cells(
        [
            {"max_lat": 0.1, "max_lon": 0.1, "max_c": 50.0},
            {"max_lat": 13.6, "max_lon": 40.6, "max_c": 90.0},
            {"max_lat": 31.6, "max_lon": 35.6, "max_c": 70.0},
        ]
    )
    assert cells == [(13.5, 40.5), (31.5, 35.5), (0.0, 0.0)]


def test_the_backfill_asks_about_each_cell_once():
    cells = backfill_cells(
        [
            {"max_lat": 13.51, "max_lon": 40.51, "max_c": 90.0},
            {"max_lat": 13.60, "max_lon": 40.60, "max_c": 80.0},
        ]
    )
    assert cells == [(13.5, 40.5)]


def test_the_backfill_skips_rows_it_cannot_place():
    assert backfill_cells([{"max_c": 90.0}, {"max_lat": None, "max_lon": 1.0}]) == []


def test_the_backfill_names_every_row_in_a_cell_with_one_update():
    writer = FakeWriter(
        unnamed={
            "alltime_readings": [{"max_lat": 13.59, "max_lon": 40.67, "max_c": 90.37}],
            "anomaly_readings": [{"max_lat": 31.6, "max_lon": 35.6, "max_c": 80.0}],
        }
    )
    session = FakeSession(
        answers={
            (13.5, 40.5): {"address": {"region": "Afar", "country": "Ethiopia"}},
            (31.5, 35.5): {"address": {"state": "Al Karak", "country": "Jordan"}},
        }
    )
    resolver = resolver_with(session=session, writer=writer)

    named = backfill(resolver, writer)

    assert named == 2
    # Both tables are patched for both cells: a cell's bounds are what decide
    # which rows it names, not which table the row was read from.
    assert len(writer.patches) == 4
    tables = {table for table, _, _, _, _ in writer.patches}
    assert tables == {"alltime_readings", "anomaly_readings"}

    afar = [patch for patch in writer.patches if patch[2] == "Afar, Ethiopia"]
    assert afar[0][1] == (13.25, 13.75, 40.25, 40.75)
    # The country rides along in the same payload, so a row is never named
    # without the country the leaderboard filters it by.
    assert afar[0][3] == "Ethiopia"

    karak = [patch for patch in writer.patches if patch[2] == "Al Karak, Jordan"]
    assert karak[0][3] == "Jordan"

    # The anomalies patch always carries the volcanic exclusion.
    for table, _, _, _, extra in writer.patches:
        assert ("cause=neq.volcanic" in extra) == (table == "anomaly_readings")


def test_the_backfill_patches_nothing_for_a_cell_with_no_name():
    writer = FakeWriter(
        unnamed={"alltime_readings": [{"max_lat": 0.0, "max_lon": -30.0, "max_c": 45.0}]}
    )
    session = FakeSession(default={"error": "Unable to geocode"})
    resolver = resolver_with(session=session, writer=writer)

    assert backfill(resolver, writer) == 0
    assert writer.patches == []
    # But the null is cached, so a re-run does not ask the ocean again.
    assert writer.upserted[0]["place_name"] is None


def test_a_second_backfill_run_asks_the_network_nothing():
    """Re-runnable by construction: the cache answers, and the row filter
    means the rows already named are not read back at all."""
    writer = FakeWriter(
        cached={(13.5, 40.5): ("Afar, Ethiopia", "Ethiopia")},
        unnamed={"alltime_readings": [{"max_lat": 13.59, "max_lon": 40.67, "max_c": 90.37}]},
    )
    session = FakeSession()
    resolver = resolver_with(session=session, writer=writer)

    assert backfill(resolver, writer) == 1
    assert session.calls == []
    assert writer.patches[0][2] == "Afar, Ethiopia"
    assert writer.patches[0][3] == "Ethiopia"


def test_a_backfill_dry_run_touches_nothing(capsys):
    writer = FakeWriter(
        unnamed={"alltime_readings": [{"max_lat": 13.59, "max_lon": 40.67, "max_c": 90.37}]}
    )
    session = FakeSession()
    resolver = resolver_with(session=session, writer=writer, dry_run=True)

    assert backfill(resolver, writer, dry_run=True) == 0
    assert session.calls == []
    assert writer.patches == []
    assert "would be reverse geocoded" in capsys.readouterr().out


def test_a_place_unpacks_onto_a_daily_row():
    """The plural builders take the mapping and unpack the pair themselves.

    This is the seam where the geocoder's Place meets the writer's columns, and
    it works only because a Place is exactly (place_name, country).
    """
    rows = build_reading_rows(
        [reading()],
        date(2026, 8, 30),
        TERRA,
        {(13, 40): Place("Afar, Ethiopia", "Ethiopia")},
    )

    assert rows[0]["place_name"] == "Afar, Ethiopia"
    assert rows[0]["country"] == "Ethiopia"


def test_a_daily_row_with_no_resolved_place_carries_two_nulls():
    rows = build_reading_rows([reading()], date(2026, 8, 30), TERRA, {})
    assert rows[0]["place_name"] is None and rows[0]["country"] is None


def test_a_place_unpacks_onto_an_anomaly_row():
    fire = Anomaly(tile=reading(), cause=CAUSE_WILDFIRE)
    rows = build_anomaly_rows(
        [fire], date(2026, 8, 30), TERRA, {fire.key: Place("Afar, Ethiopia", "Ethiopia")}
    )

    assert rows[0]["place_name"] == "Afar, Ethiopia"
    assert rows[0]["country"] == "Ethiopia"


def test_a_volcanic_row_is_left_with_both_columns_null():
    # anomaly_places never returns a key for it, so the builder finds nothing.
    lava = Anomaly(tile=reading(), cause=CAUSE_VOLCANIC, source_slug="erta-ale")
    rows = build_anomaly_rows([lava], date(2026, 8, 30), TERRA, {})

    assert rows[0]["place_name"] is None and rows[0]["country"] is None
    assert rows[0]["source_slug"] == "erta-ale"


def test_a_place_unpacks_onto_an_alltime_row():
    place = Place("Afar, Ethiopia", "Ethiopia")
    row = build_alltime_row(
        reading(), date(2026, 8, 30), TERRA, place_name=place.name, country=place.country
    )
    assert row["place_name"] == "Afar, Ethiopia"
    assert row["country"] == "Ethiopia"


class RecordingRestSession:
    """Captures what the real PostgREST client would have sent."""

    def __init__(self, payload=None):
        self.requests: list[tuple] = []
        self.payload = payload if payload is not None else []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return FakeResponse(self.payload)


def test_the_cell_patch_sends_both_columns():
    """The payload the backfill actually puts on the wire.

    The fakes above check that the backfill decides to patch; this checks the
    request that decision produces, which is the part the database sees.
    """
    session = RecordingRestSession()
    writer = SupabaseWriter(session, "service-key")

    writer.patch_place_name_in_cell(
        "alltime_readings",
        cell_bounds((13.5, 40.5)),
        "Afar, Ethiopia",
        "Ethiopia",
        "&cause=neq.volcanic",
    )

    method, url, kwargs = session.requests[0]
    assert method == "PATCH"
    assert kwargs["json"] == {"place_name": "Afar, Ethiopia", "country": "Ethiopia"}

    # Addressed by the cell's bounds, and only over rows not already named.
    assert "max_lat=gte.13.25" in url and "max_lat=lt.13.75" in url
    assert "max_lon=gte.40.25" in url and "max_lon=lt.40.75" in url
    assert "place_name=is.null" in url
    assert "cause=neq.volcanic" in url


def test_a_cell_with_no_country_patches_a_null():
    session = RecordingRestSession()
    writer = SupabaseWriter(session, "service-key")

    writer.patch_place_name_in_cell(
        "alltime_readings", cell_bounds((31.5, -117.5)), "Baja California", None
    )

    assert session.requests[0][2]["json"] == {
        "place_name": "Baja California",
        "country": None,
    }


def test_the_cache_read_asks_for_the_country_column():
    session = RecordingRestSession(
        payload=[
            {"cell_lat": 13.5, "cell_lon": 40.5, "place_name": "Afar, Ethiopia",
             "country": "Ethiopia"}
        ]
    )
    writer = SupabaseWriter(session, "service-key")

    known = writer.fetch_place_names([(13.5, 40.5)])

    assert "select=cell_lat,cell_lon,place_name,country" in session.requests[0][1]
    # Returned as a plain pair, which is exactly a Place.
    assert known == {(13.5, 40.5): ("Afar, Ethiopia", "Ethiopia")}
    assert Place(*known[(13.5, 40.5)]) == Place("Afar, Ethiopia", "Ethiopia")


def test_a_cache_row_written_before_countries_reads_back_as_no_country():
    # The column was added to a table that already had rows in it.
    session = RecordingRestSession(
        payload=[{"cell_lat": 13.5, "cell_lon": 40.5, "place_name": "Afar, Ethiopia"}]
    )
    writer = SupabaseWriter(session, "service-key")

    assert writer.fetch_place_names([(13.5, 40.5)]) == {
        (13.5, 40.5): ("Afar, Ethiopia", None)
    }


def test_the_backfill_entry_point_needs_a_reason_to_run():
    assert geocode.main([]) == 2


def test_the_backfill_entry_point_refuses_without_a_service_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    assert geocode.main(["--backfill"]) == 2
