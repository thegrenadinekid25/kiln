"""Reverse geocoding: the place names Kiln shows before coordinates.

Decision 2026-09-02. "13.59 N, 40.67 E" tells a reader nothing; "Afar Region,
Ethiopia" tells them where on Earth the hottest ground is. Every reading worth
displaying gets a name resolved here, and a null name is an honest answer the
site renders as coordinates rather than as a guess.

Three things keep this cheap and polite:

* **Half-degree cells.** Names are resolved per 0.5-degree cell, not per
  reading. A cell is roughly 55 km, far finer than the province-level name we
  ask for, so one lookup serves every reading in the neighbourhood and the same
  desert answers once rather than daily.
* **Cache first.** ``kiln.place_names`` is consulted before the network, and a
  null result is cached too -- otherwise every ocean pixel would be asked about
  again every day, forever.
* **One request per second, identified.** Nominatim is a donated public
  service. Its usage policy asks for at most one request a second and a real
  User-Agent naming the application; both are enforced here rather than left to
  the caller to remember.

Everything that decides a name is a pure function of a response payload, so the
whole naming policy is testable without a network.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

from .science import CAUSE_VOLCANIC, Anomaly, TileMax
from .supabase_io import (
    ALLTIME_TABLE,
    ANOMALIES_TABLE,
    SupabaseWriter,
    build_place_name_row,
)

LOG = logging.getLogger(__name__)

CellKey = tuple[float, float]

# One lookup serves a neighbourhood. Half a degree is about 55 km at the
# equator and less toward the poles -- well inside the area a province-level
# name covers, so a shared answer is a correct answer, not an approximation.
CELL_DEGREES = 0.5

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# Nominatim's usage policy requires an identifying User-Agent. A generic one
# gets the whole service blocked for everybody, so this names the product and
# the site it runs, which is what a rate-limit complaint would need to reach us.
USER_AGENT = (
    "TheHottestPlaceInTheWorld/1.0 (thehottestplaceintheworld.bytortoise.com)"
)

# The policy's hard limit is one request per second. Nothing here is urgent
# enough to shave it.
MIN_REQUEST_INTERVAL_S = 1.0

REQUEST_TIMEOUT_S = 20

# Zoom 8 is roughly county/province level, which is the granularity a
# half-degree cell can honestly support.
NOMINATIM_ZOOM = 8

# A second, coarser attempt for cells zoom 8 comes back empty for -- roughly
# region/country level. Land with no county or province name at zoom 8 almost
# always still sits inside a named country, so this closes most of what would
# otherwise be a permanently unnamed cell.
NOMINATIM_ZOOM_FALLBACK = 5

SOURCE = "nominatim"

# How many of the day's hottest daily rows get a name. The map shows a handful
# at a time and the archive covers the rest, so naming every hot tile on Earth
# would spend the rate limit on rows nobody reads.
DAILY_PLACE_NAME_LIMIT = 25

# Address components, most specific worth showing first. Nominatim fills
# different keys in different countries, so each rung tries several.
REGION_KEYS = ("state", "province", "region", "state_district")
COUNTY_KEYS = ("county", "municipality", "district")
COUNTRY_KEYS = ("country",)


# --- Cells ---------------------------------------------------------------------------


def _round_half_up(value: float) -> float:
    """Nearest half degree, with an exact half always going up.

    Deliberately not :func:`round`, which rounds halves to even: 13.25 would
    land in cell 13.0 and 13.75 in cell 14.0, so the cell a coordinate belongs
    to would depend on which half-degree it sat between. The backfill names a
    cell's rows by the bounds in :func:`cell_bounds`, and a coordinate that
    rounds one way while the bounds say the other would be given a neighbouring
    cell's name.
    """
    return math.floor(value / CELL_DEGREES + 0.5) * CELL_DEGREES


def cell_key(lat: float, lon: float) -> CellKey:
    """The half-degree cell a coordinate falls in.

    Rounded to the nearest half degree rather than floored, so the cell centre
    is the coordinate's nearest grid point and neighbouring readings on either
    side of a boundary share one. Exactly on a boundary goes to the northern
    and eastern cell, matching the half-open bounds in :func:`cell_bounds`.
    """
    # Adding 0.0 collapses -0.0, which is the same cell as 0.0 and would
    # otherwise key and cache twice.
    return (_round_half_up(float(lat)) + 0.0, _round_half_up(float(lon)) + 0.0)


def cell_bounds(cell: CellKey) -> tuple[float, float, float, float]:
    """``(lat_min, lat_max, lon_min, lon_max)`` of a cell, half-open at the top.

    The inverse of :func:`cell_key`: every coordinate inside these bounds rounds
    to this cell, which is what lets the backfill name a cell's rows with one
    range query instead of one request per row.
    """
    half = CELL_DEGREES / 2.0
    return (cell[0] - half, cell[0] + half, cell[1] - half, cell[1] + half)


# --- Naming policy -------------------------------------------------------------------


def _first(address: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class Place(NamedTuple):
    """What one cell resolved to: what to show, and which country it is in.

    A plain pair on purpose. The row builders take it as a two-tuple, so the
    writer never has to import this module and the dependency runs one way.

    The two are independent. A cell can have a name and no country -- Nominatim
    sometimes omits it at sea borders -- and the country is stored separately
    rather than parsed back out of the name, because "Al Karak, Jordan" split on
    a comma is a guess and ``address.country`` is an answer.
    """

    name: str | None = None
    country: str | None = None


def country_name(payload: Any) -> str | None:
    """The country of one Nominatim reverse response, or None.

    Kept apart from the display name because the leaderboard filters on it: a
    filter wants the country exactly as Nominatim spells it, not whatever
    fragment happened to end up in the display string.
    """
    if not isinstance(payload, Mapping) or payload.get("error"):
        return None
    address = payload.get("address")
    if not isinstance(address, Mapping):
        return None
    return _first(address, COUNTRY_KEYS)


def place_for(payload: Any) -> Place:
    """The name and country one reverse response yields."""
    return Place(name=display_name(payload), country=country_name(payload))


def display_name(payload: Any) -> str | None:
    """The name to show for one Nominatim reverse response, or None.

    Province and country: "Eastern Province, Saudi Arabia". A reader wants to
    know where on Earth this is, and at half-degree resolution anything finer
    would be more precise than the cell it stands for.

    Falls back to county and country, then to the country alone, then to
    nothing -- which is the right answer over ocean and empty desert, where the
    site shows coordinates instead of inventing a name.
    """
    if not isinstance(payload, Mapping) or payload.get("error"):
        return None

    address = payload.get("address")
    if not isinstance(address, Mapping):
        return None

    country = _first(address, COUNTRY_KEYS)
    for part in (_first(address, REGION_KEYS), _first(address, COUNTY_KEYS)):
        # The equality guard is for city-states, where the region and the
        # country are the same word and "Singapore, Singapore" reads as a bug.
        if part and country and part != country:
            return f"{part}, {country}"

    return country or _first(address, REGION_KEYS) or _first(address, COUNTY_KEYS)


# --- The resolver --------------------------------------------------------------------


class PlaceNameResolver:
    """Cache-first, rate-limited reverse geocoding for a run.

    One resolver per process holds everything it has already looked up, so a
    cell wanted by both an all-time row and a daily row is fetched once and
    asked about once.
    """

    def __init__(
        self,
        service_key: str | None = None,
        session: Any = None,
        writer: Any = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        user_agent: str = USER_AGENT,
        timeout: int = REQUEST_TIMEOUT_S,
    ):
        self._service_key = service_key or ""
        self._session = session
        self._writer = writer
        self._dry_run = dry_run
        self._sleep = sleep
        self._min_interval = min_interval_s
        self._user_agent = user_agent
        self._timeout = timeout

        self._known: dict[CellKey, Place] = {}
        self._requests_made = 0

        # Dry runs resolve nothing and record what they would have asked about.
        self.pending: list[CellKey] = []

    # -- plumbing the caller does not have to supply ----------------------------------

    @property
    def session(self) -> Any:
        if self._session is None:
            import requests  # noqa: PLC0415 - keeps import cost off --help

            self._session = requests.Session()
        return self._session

    @property
    def writer(self) -> Any:
        """The cache client, or None when there is no service key to use one."""
        if self._writer is None and self._service_key:
            self._writer = SupabaseWriter(self.session, self._service_key)
        return self._writer

    @property
    def requests_made(self) -> int:
        return self._requests_made

    # -- the one method callers need ---------------------------------------------------

    def resolve(self, cells: Iterable[CellKey]) -> dict[CellKey, Place]:
        """Places for these cells, reading the cache and asking for the rest."""
        wanted = {cell_key(lat, lon) for lat, lon in cells}
        missing = sorted(wanted - self._known.keys())

        if missing and self.writer is not None:
            try:
                cached = self.writer.fetch_place_names(missing)
                self._known.update(
                    {cell: Place(*pair) for cell, pair in cached.items()}
                )
            except Exception as exc:  # noqa: BLE001 - a cold cache is not fatal
                LOG.warning("place name cache unreadable (%s); geocoding from scratch", exc)
            missing = [cell for cell in missing if cell not in self._known]

        if missing and self._dry_run:
            for cell in missing:
                if cell not in self.pending:
                    self.pending.append(cell)
            return self._view(wanted)

        resolved: dict[CellKey, Place] = {}
        for cell in missing:
            place, answered = self._ask(cell, NOMINATIM_ZOOM)
            if answered and place.name is None:
                # Empty at county/province level; land almost always still has
                # a country or region name one zoom level out, so try once more
                # before giving up on the cell.
                place, answered = self._ask(cell, NOMINATIM_ZOOM_FALLBACK)
            if not answered:
                continue
            self._known[cell] = place
            resolved[cell] = place

        if resolved:
            self._store(resolved)

        return self._view(wanted)

    def _view(self, wanted: Iterable[CellKey]) -> dict[CellKey, Place]:
        """What is known about these cells; unresolved ones read as unnamed."""
        return {cell: self._known.get(cell, Place()) for cell in wanted}

    def _store(self, resolved: Mapping[CellKey, Place]) -> None:
        if self.writer is None:
            return
        rows = [
            build_place_name_row(cell, place.name, place.country, SOURCE)
            for cell, place in sorted(resolved.items())
        ]
        try:
            self.writer.upsert_place_names(rows)
        except Exception as exc:  # noqa: BLE001 - the names are already in hand
            LOG.warning(
                "could not cache %d place name(s) (%s); this run still uses them, "
                "but tomorrow's will ask again",
                len(rows),
                exc,
            )

    def _ask(self, cell: CellKey, zoom: int) -> tuple[Place, bool]:
        """Reverse geocode one cell at one zoom level.

        Returns ``(place, answered)``. ``answered`` is False when Nominatim did
        not reply with a usable response -- a timeout, a rate limit, an outage.
        Those are not knowledge about the place and must not be cached: a null
        cached during an outage would be permanent, and the cell would never be
        asked about again. A 200 that names nothing IS knowledge, and its null
        is cached exactly so we stop asking -- callers that want a coarser
        second try before accepting that null make it themselves, as
        :meth:`resolve` does at :data:`NOMINATIM_ZOOM_FALLBACK`.

        Every call is a real request and goes through the same rate limiter,
        whether it is a cell's first attempt or a fallback retry.
        """
        if self._requests_made:
            self._sleep(self._min_interval)
        self._requests_made += 1

        try:
            response = self.session.get(
                NOMINATIM_URL,
                params={
                    "format": "jsonv2",
                    "lat": f"{cell[0]:.4f}",
                    "lon": f"{cell[1]:.4f}",
                    "zoom": zoom,
                    "accept-language": "en",
                },
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - one cell must not end the run
            LOG.warning("reverse geocode of %s failed (%s); leaving it unnamed", cell, exc)
            return Place(), False

        status = int(getattr(response, "status_code", 200))
        if status != 200:
            LOG.warning(
                "reverse geocode of %s returned HTTP %d; leaving it unnamed and "
                "uncached so a later run can ask again",
                cell,
                status,
            )
            return Place(), False

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - an unreadable body is not an answer
            LOG.warning("reverse geocode of %s returned no usable JSON (%s)", cell, exc)
            return Place(), False

        return place_for(payload), True


# --- Wiring helpers ------------------------------------------------------------------


def tile_places(
    resolver: PlaceNameResolver, tiles: Iterable[TileMax]
) -> dict[tuple[int, int], Place]:
    """Places for a set of readings, keyed the way the row builders want them."""
    listed = list(tiles)
    resolved = resolver.resolve(cell_key(t.max_lat, t.max_lon) for t in listed)
    return {
        t.key: resolved.get(cell_key(t.max_lat, t.max_lon), Place()) for t in listed
    }


def anomaly_places(
    resolver: PlaceNameResolver, anomalies: Iterable[Anomaly]
) -> dict[tuple[int, int, str], Place]:
    """Places for anomaly rows, skipping the volcanic ones.

    A volcanic row already knows exactly where it is -- it carries the slug of
    a curated, cited vent, and the site names it and its country from that.
    Asking Nominatim what is at the same coordinate would spend a request to
    learn something vaguer than what the row already holds.
    """
    named = [a for a in anomalies if a.cause != CAUSE_VOLCANIC]
    resolved = resolver.resolve(cell_key(a.tile.max_lat, a.tile.max_lon) for a in named)
    return {
        a.key: resolved.get(cell_key(a.tile.max_lat, a.tile.max_lon), Place())
        for a in named
    }


def report_pending(resolver: PlaceNameResolver) -> None:
    """Print the cells a real run would have asked Nominatim about."""
    print("\nplace names (dry run, nothing geocoded)")
    if not resolver.pending:
        print("  every cell needed was already in the kiln.place_names cache")
        return

    seconds = len(resolver.pending) * MIN_REQUEST_INTERVAL_S
    print(
        f"  {len(resolver.pending)} half-degree cell(s) would be reverse geocoded, "
        f"about {seconds:.0f}s at {MIN_REQUEST_INTERVAL_S:.0f} request/second"
    )
    for cell in resolver.pending[:20]:
        print(f"  cell {cell[0]:7.1f},{cell[1]:8.1f}")
    if len(resolver.pending) > 20:
        print(f"  ... and {len(resolver.pending) - 20} more")


# --- Backfill ------------------------------------------------------------------------

# Anomaly rows are geocoded except the volcanic ones, which the site names from
# the vent list instead.
NON_VOLCANIC = f"&cause=neq.{CAUSE_VOLCANIC}"


def backfill_cells(rows: Iterable[Mapping[str, Any]]) -> list[CellKey]:
    """The distinct cells a set of unnamed rows needs, hottest first.

    Ordered by temperature so an interrupted backfill has already named the
    readings anyone is most likely to be looking at.
    """
    ordered = sorted(
        rows, key=lambda row: -float(row.get("max_c") or 0.0)
    )
    cells: list[CellKey] = []
    seen: set[CellKey] = set()
    for row in ordered:
        try:
            cell = cell_key(float(row["max_lat"]), float(row["max_lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if cell not in seen:
            seen.add(cell)
            cells.append(cell)
    return cells


def backfill(
    resolver: PlaceNameResolver,
    writer: Any,
    dry_run: bool = False,
) -> int:
    """Fill in place_name for existing archive and anomaly rows. Re-runnable.

    Reads only the rows still missing a name, resolves their cells cache-first,
    and names each cell's rows with one range update. Everything about it is
    idempotent: a second run finds fewer unnamed rows, asks the network about
    none of the cells it already resolved, and patches nothing it already
    patched.
    """
    targets: list[tuple[str, str, list[Mapping[str, Any]]]] = []
    for table, extra in ((ALLTIME_TABLE, ""), (ANOMALIES_TABLE, NON_VOLCANIC)):
        # The volcanic filter belongs on the read as well as the write, or those
        # rows would drag their cells into the request budget to no purpose.
        rows = writer.fetch_unnamed_rows(table, "max_lat,max_lon,max_c", extra)
        LOG.info("backfill: %d unnamed row(s) in %s", len(rows), table)
        targets.append((table, extra, rows))

    cells = backfill_cells([row for _, _, rows in targets for row in rows])
    LOG.info("backfill: %d distinct half-degree cell(s) to resolve", len(cells))

    # Resolving first is what fills in ``pending``: a dry-run resolver reads the
    # cache and records the misses without asking the network about any of them.
    named = resolver.resolve(cells)

    if dry_run:
        report_pending(resolver)
        return 0

    patched = 0
    for cell in cells:
        place = named.get(cell, Place())
        if not place.name:
            # Nothing to say about this cell. The country alone is never enough:
            # a row with a country and no name would render as an unnamed
            # reading that the leaderboard could still filter, which is worse
            # than showing coordinates.
            continue
        for table, extra, _ in targets:
            writer.patch_place_name_in_cell(
                table, cell_bounds(cell), place.name, place.country, extra
            )
        patched += 1

    LOG.info(
        "backfill: named %d of %d cell(s); the rest had no name to give",
        patched,
        len(cells),
    )
    return patched


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kiln_ingest.geocode",
        description=(
            "Fill in place_name for rows written before reverse geocoding existed. "
            "Cache-first and rate-limited; safe to re-run."
        ),
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Name the existing alltime_readings and anomaly_readings rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cells that would be geocoded and touch nothing.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity. Default INFO.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.backfill:
        LOG.error("nothing to do; pass --backfill")
        return 2

    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not service_key:
        LOG.error("SUPABASE_SERVICE_KEY is not set; the backfill reads and writes rows")
        return 2

    import requests  # noqa: PLC0415 - keeps import cost off --help

    with requests.Session() as session:
        writer = SupabaseWriter(session, service_key)
        resolver = PlaceNameResolver(
            service_key=service_key,
            session=session,
            writer=writer,
            dry_run=args.dry_run,
        )
        try:
            backfill(resolver, writer, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - reported through the exit code
            LOG.exception("backfill failed: %s", exc)
            return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
