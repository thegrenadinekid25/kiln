"""Writes to the kiln schema through the Supabase REST (PostgREST) API.

Kiln's ingestion pipeline is the only writer; the public site reads with the
anon key under RLS. We use plain HTTP rather than supabase-py to keep the
dependency list to numpy/pyhdf/requests.

Payload construction is separated from transport so the row shapes can be
tested without a network or a service key.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .cmr import satellite_for_product
from .science import Anomaly, TileMax

LOG = logging.getLogger(__name__)

SUPABASE_URL = "https://wdvguesfxcxxatzpirvy.supabase.co"
REST_BASE = f"{SUPABASE_URL}/rest/v1/"

# Kiln lives in its own schema inside the shared tortoise project, so every
# request has to name the schema explicitly.
SCHEMA = "kiln"

READINGS_TABLE = "lst_readings"
RUNS_TABLE = "ingest_runs"
ALLTIME_TABLE = "alltime_readings"
ANOMALIES_TABLE = "anomaly_readings"
PLACE_NAMES_TABLE = "place_names"

PLACE_NAMES_ON_CONFLICT = "cell_lat,cell_lon"

# How many half-degree cells to ask the cache about in one URL. Each term is
# about forty characters, so this keeps a query string well inside what any
# proxy will carry.
CACHE_QUERY_CHUNK = 50

ON_CONFLICT = "reading_date,product,tile_lat,tile_lon"

# Cause is part of the key: one tile can be two different non-weather things on
# one day -- a rejected record-tier reading and the vent that produced it -- and
# each gets its own row rather than one overwriting the other.
ANOMALY_ON_CONFLICT = "reading_date,product,tile_lat,tile_lon,cause"

# The all-time table holds one row per place, ever: no date and no product in
# the key, because a tile's record belongs to whichever day and satellite set
# it. record_date says which day that was.
ALLTIME_ON_CONFLICT = "tile_lat,tile_lon"

# PostgREST caps a response at 1000 rows; the archive grows past that.
SELECT_PAGE_SIZE = 1000
SELECT_MAX_PAGES = 100

# PostgREST rejects very large bodies and a full hot day can be a few thousand
# tiles; upsert in batches.
UPSERT_BATCH_SIZE = 500

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


class SupabaseWriteError(RuntimeError):
    """A write to the kiln schema was rejected."""


def service_headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Content-Profile": SCHEMA,
        "Accept-Profile": SCHEMA,
    }


def build_reading_row(
    tile: TileMax,
    reading_date: date,
    product: str,
    place_name: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """One lst_readings row. max_c is numeric(5,2) in the schema.

    ``place_name`` is null whenever the geocoder had nothing to say, which the
    site reads as "show the coordinates instead". Rows are written with the
    column present either way, so an absent name is recorded as absent rather
    than left to whatever the row held before. ``country`` is the same, and is
    what the leaderboard's country filter reads.
    """
    return {
        "reading_date": reading_date.isoformat(),
        "satellite": satellite_for_product(product),
        "product": product,
        "tile_lat": int(tile.tile_lat),
        "tile_lon": int(tile.tile_lon),
        "max_c": round(float(tile.max_c), 2),
        "max_lat": round(float(tile.max_lat), 6),
        "max_lon": round(float(tile.max_lon), 6),
        "observed_at": tile.observed_at,
        "granule_id": tile.granule_id,
        "qc_note": tile.qc_note,
        "place_name": place_name,
        "country": country,
    }


# A resolved cell: (place_name, country). Taken as a plain pair so this module
# never imports the geocoder, whose Place is exactly this shape.
PlacePair = tuple[str | None, str | None]
NO_PLACE: PlacePair = (None, None)


def build_reading_rows(
    tiles: Iterable[TileMax],
    reading_date: date,
    product: str,
    places: Mapping[tuple[int, int], PlacePair] | None = None,
) -> list[dict[str, Any]]:
    """Daily rows, each carrying the place resolved for its tile, if any."""
    resolved = places or {}
    return [
        build_reading_row(tile, reading_date, product, *resolved.get(tile.key, NO_PLACE))
        for tile in tiles
    ]


def build_anomaly_row(
    anomaly: Anomaly,
    reading_date: date,
    product: str,
    place_name: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """One anomaly_readings row.

    The measurement fields are built by the same function the weather rows use,
    so a reading routed out of the archive is the same number written the same
    way -- only the columns saying what it is are added.

    Volcanic rows carry no place name and no country by design: they carry a
    ``source_slug`` instead, and the site names them from the curated vent list,
    which is more specific and better cited than a reverse geocode of the same
    coordinate.
    """
    row = build_reading_row(anomaly.tile, reading_date, product, place_name, country)
    row["cause"] = anomaly.cause
    row["source_slug"] = anomaly.source_slug
    return row


def build_anomaly_rows(
    anomalies: Iterable[Anomaly],
    reading_date: date,
    product: str,
    places: Mapping[tuple[int, int, str], PlacePair] | None = None,
) -> list[dict[str, Any]]:
    resolved = places or {}
    return [
        build_anomaly_row(
            anomaly, reading_date, product, *resolved.get(anomaly.key, NO_PLACE)
        )
        for anomaly in anomalies
    ]


def build_alltime_row(
    tile: TileMax,
    record_date: date,
    product: str,
    updated_at: datetime | None = None,
    place_name: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """One alltime_readings row: a tile's record and the day that set it."""
    updated = updated_at or datetime.now(timezone.utc)
    return {
        "record_date": record_date.isoformat(),
        "satellite": satellite_for_product(product),
        "product": product,
        "tile_lat": int(tile.tile_lat),
        "tile_lon": int(tile.tile_lon),
        "max_c": round(float(tile.max_c), 2),
        "max_lat": round(float(tile.max_lat), 6),
        "max_lon": round(float(tile.max_lon), 6),
        "observed_at": tile.observed_at,
        "granule_id": tile.granule_id,
        "qc_note": tile.qc_note,
        "updated_at": updated.astimezone(timezone.utc).isoformat(),
        "place_name": place_name,
        "country": country,
    }


def build_place_name_row(
    cell: tuple[float, float],
    place_name: str | None,
    country: str | None,
    source: str,
    resolved_at: datetime | None = None,
) -> dict[str, Any]:
    """One place_names cache row.

    A null ``place_name`` is a real answer and is stored as one: it says the
    geocoder was asked about this cell and had nothing, so tomorrow's run does
    not ask again. Ocean and deep desert are the common cases.

    ``country`` is cached beside the name rather than derived from it, so the
    leaderboard's country filter reads the same string the geocoder returned.
    """
    resolved = resolved_at or datetime.now(timezone.utc)
    return {
        "cell_lat": round(float(cell[0]), 1),
        "cell_lon": round(float(cell[1]), 1),
        "place_name": place_name,
        "country": country,
        "source": source,
        "resolved_at": resolved.astimezone(timezone.utc).isoformat(),
    }


def cell_filter(cells: Sequence[tuple[float, float]]) -> str:
    """A PostgREST ``or=`` filter matching exactly these cells."""
    terms = ",".join(
        f"and(cell_lat.eq.{lat:.1f},cell_lon.eq.{lon:.1f})" for lat, lon in cells
    )
    return f"or=({terms})"


def build_run_start_row(reading_date: date, product: str) -> dict[str, Any]:
    return {
        "reading_date": reading_date.isoformat(),
        "product": product,
        "status": STATUS_RUNNING,
    }


def resolve_run_status(
    granules_total: int, granules_processed: int, fatal_error: str | None = None
) -> str:
    """succeeded / partial / failed, from the granule tallies."""
    if fatal_error is not None:
        return STATUS_FAILED
    if granules_total == 0 or granules_processed == 0:
        return STATUS_FAILED
    if granules_processed < granules_total:
        return STATUS_PARTIAL
    return STATUS_SUCCEEDED


def build_run_finish_row(
    granules_total: int,
    granules_processed: int,
    tiles_written: int,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    finished = finished_at or datetime.now(timezone.utc)
    return {
        "finished_at": finished.isoformat(),
        "status": resolve_run_status(granules_total, granules_processed, error),
        "granules_total": int(granules_total),
        "granules_processed": int(granules_processed),
        "tiles_written": int(tiles_written),
        "error": error,
    }


def batched(rows: Sequence[Any], size: int = UPSERT_BATCH_SIZE):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


class SupabaseWriter:
    """Thin PostgREST client scoped to the kiln schema."""

    def __init__(
        self,
        session: Any,
        service_key: str,
        timeout: int = 60,
        attempts: int = 5,
        sleep: Any = time.sleep,
    ):
        if not service_key:
            raise SupabaseWriteError("SUPABASE_SERVICE_KEY is empty")
        self._session = session
        self._headers = service_headers(service_key)
        self._timeout = timeout
        self._attempts = attempts
        self._sleep = sleep

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One PostgREST call; 429/5xx/transport retried with backoff.

        The REST API rides the same shared connection pool as storage, so it
        gets the same patience. Semantic 4xx errors fail immediately.
        """
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}) or {})
        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._session.request(
                    method, f"{REST_BASE}{path}", headers=headers, timeout=self._timeout, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - transport failures are retryable
                last_error = exc
                if attempt < self._attempts:
                    self._sleep(min(2.0 ** attempt, 30.0))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = SupabaseWriteError(
                    f"{method} {path} failed with {response.status_code}: {response.text[:500]}"
                )
                if attempt < self._attempts:
                    self._sleep(min(2.0 ** attempt, 30.0))
                continue
            if response.status_code >= 400:
                raise SupabaseWriteError(
                    f"{method} {path} failed with {response.status_code}: {response.text[:500]}"
                )
            return response
        raise SupabaseWriteError(
            f"{method} {path} failed after {self._attempts} attempts: {last_error}"
        )

    def start_run(self, reading_date: date, product: str) -> int:
        response = self._request(
            "POST",
            RUNS_TABLE,
            json=build_run_start_row(reading_date, product),
            headers={"Prefer": "return=representation"},
        )
        body = response.json()
        if not body:
            raise SupabaseWriteError("ingest_runs insert returned no row")
        return int(body[0]["id"])

    def finish_run(
        self,
        run_id: int,
        granules_total: int,
        granules_processed: int,
        tiles_written: int,
        error: str | None = None,
    ) -> str:
        payload = build_run_finish_row(
            granules_total, granules_processed, tiles_written, error
        )
        self._request(
            "PATCH",
            f"{RUNS_TABLE}?id=eq.{run_id}",
            json=payload,
            headers={"Prefer": "return=minimal"},
        )
        return payload["status"]

    def upsert_readings(
        self,
        tiles: Sequence[TileMax],
        reading_date: date,
        product: str,
        places: Mapping[tuple[int, int], PlacePair] | None = None,
    ) -> int:
        rows = build_reading_rows(tiles, reading_date, product, places)
        for batch in batched(rows):
            self._request(
                "POST",
                f"{READINGS_TABLE}?on_conflict={ON_CONFLICT}",
                json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            LOG.info("upserted %d lst_readings rows", len(batch))
        return len(rows)

    def upsert_anomalies(
        self,
        anomalies: Sequence[Anomaly],
        reading_date: date,
        product: str,
        places: Mapping[tuple[int, int, str], PlacePair] | None = None,
    ) -> int:
        """Write the day's non-weather readings for one product."""
        rows = build_anomaly_rows(anomalies, reading_date, product, places)
        for batch in batched(rows):
            self._request(
                "POST",
                f"{ANOMALIES_TABLE}?on_conflict={ANOMALY_ON_CONFLICT}",
                json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            LOG.info("upserted %d anomaly_readings rows", len(batch))
        return len(rows)

    def fetch_alltime_maxima(self) -> dict[tuple[int, int], float]:
        """Every tile's current all-time maximum, paged.

        This is what decides whether today beat the record, so it has to be the
        whole table rather than a sample: a missing row reads as "no record
        yet" and would let a cooler reading overwrite a hotter one.
        """
        maxima: dict[tuple[int, int], float] = {}
        for page in range(SELECT_MAX_PAGES):
            offset = page * SELECT_PAGE_SIZE
            response = self._request(
                "GET",
                f"{ALLTIME_TABLE}?select=tile_lat,tile_lon,max_c"
                f"&limit={SELECT_PAGE_SIZE}&offset={offset}",
            )
            rows = response.json() or []
            for row in rows:
                maxima[(int(row["tile_lat"]), int(row["tile_lon"]))] = float(row["max_c"])
            if len(rows) < SELECT_PAGE_SIZE:
                return maxima

        raise SupabaseWriteError(
            f"alltime_readings did not end after {SELECT_MAX_PAGES} pages; refusing to "
            "compare against a partial archive"
        )

    def fetch_place_names(
        self, cells: Sequence[tuple[float, float]]
    ) -> dict[tuple[float, float], tuple[str | None, str | None]]:
        """Cached ``(place_name, country)`` for exactly these cells, in chunks.

        A cell absent from the result has never been resolved; a cell present
        with a null name has been, and came back with nothing. The caller has
        to tell those apart, which is why this returns only what the table
        actually holds rather than filling in blanks.

        Returned as plain pairs rather than as the geocoder's ``Place``, so the
        dependency runs one way: the geocoder knows about the writer and the
        writer stays a transport that knows about columns.
        """
        known: dict[tuple[float, float], tuple[str | None, str | None]] = {}
        unique = sorted({(round(float(lat), 1), round(float(lon), 1)) for lat, lon in cells})

        for chunk in batched(unique, CACHE_QUERY_CHUNK):
            response = self._request(
                "GET",
                f"{PLACE_NAMES_TABLE}?select=cell_lat,cell_lon,place_name,country"
                f"&{cell_filter(chunk)}",
            )
            for row in response.json() or []:
                key = (round(float(row["cell_lat"]), 1), round(float(row["cell_lon"]), 1))
                known[key] = (row["place_name"], row.get("country"))

        return known

    def upsert_place_names(self, rows: Sequence[dict[str, Any]]) -> int:
        for batch in batched(rows):
            self._request(
                "POST",
                f"{PLACE_NAMES_TABLE}?on_conflict={PLACE_NAMES_ON_CONFLICT}",
                json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            LOG.info("upserted %d place_names rows", len(batch))
        return len(rows)

    def fetch_unnamed_rows(
        self, table: str, select: str, extra_filter: str = ""
    ) -> list[dict[str, Any]]:
        """Every row of ``table`` still missing a place name, paged.

        Used only by the backfill. Filtering server-side on ``place_name`` keeps
        a re-run proportional to what is left to do rather than to the size of
        the table.
        """
        found: list[dict[str, Any]] = []
        for page in range(SELECT_MAX_PAGES):
            offset = page * SELECT_PAGE_SIZE
            response = self._request(
                "GET",
                f"{table}?select={select}&place_name=is.null{extra_filter}"
                f"&limit={SELECT_PAGE_SIZE}&offset={offset}",
            )
            rows = response.json() or []
            found.extend(rows)
            if len(rows) < SELECT_PAGE_SIZE:
                return found

        raise SupabaseWriteError(
            f"{table} did not end after {SELECT_MAX_PAGES} pages of unnamed rows"
        )

    def patch_place_name_in_cell(
        self,
        table: str,
        bounds: tuple[float, float, float, float],
        place_name: str,
        country: str | None = None,
        extra_filter: str = "",
    ) -> None:
        """Name every still-unnamed row whose coordinates fall inside one cell.

        Addressed by the cell's own bounds rather than row by row, which is what
        makes the backfill a few hundred requests instead of a few thousand.
        The ``place_name=is.null`` filter makes a re-run a no-op over what is
        already done.

        The country goes in the same payload, so a row never ends up named
        without the country the leaderboard filters it by.
        """
        lat_min, lat_max, lon_min, lon_max = bounds
        self._request(
            "PATCH",
            f"{table}?max_lat=gte.{lat_min}&max_lat=lt.{lat_max}"
            f"&max_lon=gte.{lon_min}&max_lon=lt.{lon_max}"
            f"&place_name=is.null{extra_filter}",
            json={"place_name": place_name, "country": country},
            headers={"Prefer": "return=minimal"},
        )

    def upsert_alltime(self, rows: Sequence[dict[str, Any]]) -> int:
        for batch in batched(rows):
            self._request(
                "POST",
                f"{ALLTIME_TABLE}?on_conflict={ALLTIME_ON_CONFLICT}",
                json=batch,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            LOG.info("upserted %d alltime_readings rows", len(batch))
        return len(rows)
