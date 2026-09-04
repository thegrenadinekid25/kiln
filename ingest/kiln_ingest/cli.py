"""Command line entry point for the Kiln LST ingestion pipeline.

    python -m kiln_ingest --date 2026-08-30 [--product MOD11_L2] [--max-granules N] [--dry-run]

Runs both Terra and Aqua when --product is omitted. Reads EARTHDATA_TOKEN and
SUPABASE_SERVICE_KEY from the environment; neither is needed for --dry-run
beyond the Earthdata token, which downloads still require.

Three things happen per day. Each product is reduced to 1-degree tile maxima and
upserted into ``lst_readings``, product by product. Alongside that, every hot
pixel of both products is painted into one shared web-mercator raster pyramid,
which is encoded and published to Supabase Storage once at the end -- one
pyramid per day, the maximum across both satellites. Finally that same day is
folded into the permanent all-time archive, which answers "how hot has this
ground ever got" rather than "how hot was it yesterday".

The two once-per-day stages read the same :class:`DayAccumulator`, filled only
from pixels that already cleared the volcanic screen, the active fire mask and
the latitude plausibility screen -- and, between the product loop and the
stages, the cross-satellite corroboration screen. For the all-time archive that
ordering is the whole correctness story: a merge is a maximum and a maximum is
permanent.

What those screens set aside is not thrown away. Heat that is real but not
weather -- lava lakes, notable wildfires, record-tier readings the other
satellite contradicted or never saw -- is published into ``anomaly_readings``
with a worded cause, so the weather archive holds corroborated weather and the
rest is still shown, named for what it is (decision 2026-09-02).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from . import alltime, geocode, raster, storage_io, tile_png
from .cmr import (
    PRODUCTS,
    BoundingBox,
    GranuleRef,
    fire_product_for,
    granule_time_key,
    product_from_granule_id,
    satellite_for_product,
    search_fire_granules,
    search_granules,
    time_key_map,
)
from .download import download_granule
from .science import (
    CORROBORATION_THRESHOLD_C,
    CORROBORATION_TOLERANCE_K,
    FIRE_UNAVAILABLE_NOTE,
    HOT_TILE_THRESHOLD_C,
    QC_NOTE,
    TOP_TILE_COUNT,
    VOLCANIC_ANOMALY_MIN_C,
    WILDFIRE_ANOMALY_MIN_C,
    Anomaly,
    Corroboration,
    TileMax,
    VolcanicSource,
    corroborate_day,
    fire_exclusion_keys,
    load_volcanic_sources,
    merge_anomalies,
    merge_tile_maxima,
    select_reported_tiles,
)
from .supabase_io import (
    ANOMALIES_TABLE,
    STATUS_FAILED,
    SupabaseWriter,
    build_alltime_row,
    build_anomaly_row,
    build_anomaly_rows,
    build_reading_rows,
    resolve_run_status,
)

LOG = logging.getLogger("kiln_ingest")

# Fire granules are small and their absence is survivable, so they get fewer
# retries than an LST granule: one slow overpass is not worth stalling the day.
FIRE_DOWNLOAD_ATTEMPTS = 2

DEFAULT_TILES_DIR = Path("out-tiles")

# How long before an Earthdata token runs out we start saying so. Two weeks is
# enough for someone to notice a warning on a daily cron and act on it without
# the map ever going dark.
TOKEN_RENEWAL_WINDOW = timedelta(days=14)
TOKEN_RENEWAL_ACTION = (
    "Regenerate at urs.earthdata.nasa.gov, then update Doppler (kiln/prd) "
    "and the GitHub repo secret."
)

# One granule reduced to its per-tile maxima: (path, granule id, observed at).
GranuleReducer = Callable[[Path, str, str], dict[tuple[int, int], TileMax]]


@dataclass(frozen=True)
class Discovery:
    """Which CMR holdings to search, and where on Earth.

    The daily cron leaves both at their defaults: LANCE, whole globe. A
    historical backfill sets ``archive`` to reach the LP DAAC science-quality
    collection, which is the only one holding dates older than LANCE's window,
    and usually narrows to a few bounding boxes so a day costs a handful of
    granules instead of a couple of hundred.
    """

    archive: bool = False
    bboxes: tuple[BoundingBox, ...] = ()

    @property
    def holdings(self) -> str:
        return "LP DAAC archive" if self.archive else "LANCE near-real-time"


@dataclass
class DayAccumulator:
    """What the per-product runs contribute to the once-per-day stages.

    The raster pyramid and the all-time archive are per day and per place, not
    per satellite, so Terra and Aqua accumulate here before either is published.
    """

    raster: raster.TileStore = field(default_factory=dict)

    # Kept per product, not merged, because the cross-satellite corroboration
    # screen has to compare the two satellites against each other before either
    # once-per-day stage runs.
    per_product: dict[str, dict[tuple[int, int], TileMax]] = field(default_factory=dict)

    # The day's maxima across products. The per-product loop fills this with the
    # plain merge; :func:`main` replaces it with the corroborated version before
    # the raster or all-time stage reads it.
    tiles: dict[tuple[int, int], TileMax] = field(default_factory=dict)

    # Non-weather readings, per product, keyed by (tile lat, tile lon, cause) --
    # the unique constraint on kiln.anomaly_readings, less the date. The product
    # loop fills in the volcanic and wildfire ones as granules are reduced;
    # :func:`screen_day` adds the two corroboration causes once both satellites
    # have been seen.
    anomalies: dict[str, dict[tuple[int, int, str], Anomaly]] = field(default_factory=dict)


@dataclass
class ProductResult:
    product: str
    status: str
    granules_total: int
    granules_processed: int
    tiles_written: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != STATUS_FAILED


def yesterday_utc() -> date:
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--date must be YYYY-MM-DD, got {value!r}"
        ) from None


def parse_bbox(value: str) -> BoundingBox:
    """A ``w,s,e,n`` bounding box in degrees."""
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"--bbox must be four comma-separated degrees, west,south,east,north; got {value!r}"
        )
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--bbox coordinates must be numbers; got {value!r}"
        ) from None

    for name, degrees, limit in (
        ("west", west, 180.0),
        ("east", east, 180.0),
        ("south", south, 90.0),
        ("north", north, 90.0),
    ):
        if not -limit <= degrees <= limit:
            raise argparse.ArgumentTypeError(
                f"--bbox {name} must be within -{limit:g}..{limit:g}, got {degrees:g}"
            )
    if south >= north:
        raise argparse.ArgumentTypeError(
            f"--bbox south ({south:g}) must be below north ({north:g})"
        )
    # west > east is left alone: CMR reads that as a box crossing the antimeridian.
    return (west, south, east, north)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kiln_ingest",
        description="Ingest NASA MODIS near-real-time land surface temperature into Kiln.",
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        default=None,
        help="UTC date to ingest (YYYY-MM-DD). Defaults to yesterday UTC.",
    )
    parser.add_argument(
        "--product",
        choices=sorted(PRODUCTS),
        default=None,
        help="Single product to ingest. Default: both MOD11_L2 and MYD11_L2.",
    )
    parser.add_argument(
        "--max-granules",
        type=int,
        default=None,
        help="Cap the number of granules downloaded per product (for testing).",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Search the LP DAAC science-quality archive instead of LANCE. Required "
        "for any date older than LANCE's few-day window.",
    )
    parser.add_argument(
        "--bbox",
        type=parse_bbox,
        action="append",
        default=None,
        metavar="W,S,E,N",
        help="Only fetch granules intersecting this box, in degrees. Repeatable; "
        "repeats are ORed together. A box starting with a negative longitude "
        "needs the equals form, or argparse reads it as a flag: "
        "--bbox=-118,32,-114,36",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip all Supabase writes; print the hottest tiles and write raster "
        "tiles to --tiles-dir instead.",
    )
    parser.add_argument(
        "--tiles-dir",
        type=Path,
        default=DEFAULT_TILES_DIR,
        help=f"Where --dry-run writes the raster pyramid. Default: {DEFAULT_TILES_DIR}/.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity. Default INFO.",
    )
    return parser


def token_expiry(token: str) -> datetime | None:
    """The ``exp`` claim of an Earthdata Login JWT, or None if it cannot be read.

    The payload is read, not verified: we are not authenticating the token --
    NASA does that -- only reading the expiry date it already carries, so the
    run can name the date instead of producing a wall of 401s. Anything that is
    not a decodable JWT with a numeric ``exp`` returns None; a token we cannot
    parse is not evidence of a token that will not work.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None

    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromtimestamp(float(claims["exp"]), tz=timezone.utc)
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        # ValueError covers bad base64 (binascii.Error), bad UTF-8 and bad JSON;
        # KeyError and TypeError a payload shaped differently than expected;
        # OverflowError and OSError an exp beyond what datetime can represent.
        return None


def check_earthdata_token(token: str, now: datetime) -> bool:
    """Whether the run may proceed, warning when the token is near expiry.

    An expired token stops the run here rather than downstream: every download
    would fail with a 401, and one line naming the date and the two places the
    token lives is worth more than a hundred authentication errors. A token
    whose expiry cannot be read is never a reason to refuse to run -- the check
    exists to explain failures, not to create them.
    """
    expires = token_expiry(token)
    if expires is None:
        LOG.warning(
            "could not read an expiry date out of EARTHDATA_TOKEN; "
            "continuing without the expiry check"
        )
        return True

    expires_on = expires.date().isoformat()
    if expires <= now:
        LOG.error(
            "EARTHDATA_TOKEN expired on %s; every granule download would fail. %s",
            expires_on,
            TOKEN_RENEWAL_ACTION,
        )
        return False

    remaining = expires - now
    if remaining <= TOKEN_RENEWAL_WINDOW:
        LOG.warning(
            "EARTHDATA_TOKEN expires on %s, in %d days. %s",
            expires_on,
            remaining.days,
            TOKEN_RENEWAL_ACTION,
        )
    else:
        LOG.info("EARTHDATA_TOKEN is valid until %s", expires_on)
    return True


def discover_fire_granules(
    session, product: str, target: date, discovery: Discovery | None = None
) -> dict[str, str]:
    """Overpass stamp -> active-fire granule URL for the day.

    Returns an empty map if discovery fails. A day without fire granules is a
    day of unchecked tiles, which the qc_note says out loud; it is never a
    reason to publish no temperatures at all.
    """
    discovery = discovery or Discovery()
    fire_product = fire_product_for(product)
    try:
        refs = search_fire_granules(
            session, product, target, archive=discovery.archive, bboxes=discovery.bboxes
        )
    except Exception as exc:  # noqa: BLE001 - CMR being down must not sink the day
        LOG.warning(
            "%s discovery failed (%s); %s tiles will be marked unchecked",
            fire_product,
            exc,
            product,
        )
        return {}

    mapping = time_key_map(refs)
    LOG.info("%s: %d fire granules paired by overpass", fire_product, len(mapping))
    return mapping


def fire_exclusion_for(
    session,
    token: str,
    workdir: Path,
    fire_urls: dict[str, str] | None,
    granule_id: str,
    read_fire: Callable[[Path], tuple[np.ndarray, np.ndarray]],
) -> tuple[np.ndarray | None, str]:
    """Fire bin keys for one overpass, plus the qc_note its tiles should carry.

    ``None`` keys mean the mask could not be applied, and the note says so. An
    empty key array means the mask was applied and found nothing burning, which
    is a different and much better statement.
    """
    if fire_urls is None:
        return None, QC_NOTE

    key = granule_time_key(granule_id)
    url = fire_urls.get(key) if key is not None else None
    if url is None:
        LOG.warning("no active-fire granule matches %s; ingesting it unmasked", granule_id)
        return None, QC_NOTE + FIRE_UNAVAILABLE_NOTE

    destination = workdir / f"fire_{Path(url).name}"
    try:
        download_granule(session, url, destination, token, attempts=FIRE_DOWNLOAD_ATTEMPTS)
        fire_lat, fire_lon = read_fire(destination)
        return fire_exclusion_keys(fire_lat, fire_lon), QC_NOTE
    except Exception as exc:  # noqa: BLE001 - a missing mask never fails a granule
        LOG.warning("fire granule for %s unusable (%s); ingesting it unmasked", granule_id, exc)
        return None, QC_NOTE + FIRE_UNAVAILABLE_NOTE
    finally:
        destination.unlink(missing_ok=True)


def build_reducer(
    session,
    token: str,
    workdir: Path,
    fire_urls: dict[str, str] | None,
    raster_store: raster.TileStore | None,
    anomaly_sink: dict[tuple[int, int, str], Anomaly] | None = None,
    volcanic_sources: Sequence[VolcanicSource] | None = None,
) -> GranuleReducer:
    """The granule reducer that applies the masks and feeds the raster.

    :func:`process_granules` defaults to the plain tile-only reader; this is the
    richer path :func:`main` installs, and every other stage hangs off it. They
    all read the one masked pixel field, so a burning pixel is excluded from the
    raster and the tile maxima together or from neither -- and the anomalies it
    lands in come from that same field's exclusions.

    ``volcanic_sources`` is loaded once here rather than per granule, so a run
    says how many vents it is screening against exactly once.
    """
    # Imported here so a machine without libhdf4 can still import the CLI.
    from .granule import granule_reduction, read_fire_granule

    sources = load_volcanic_sources() if volcanic_sources is None else tuple(volcanic_sources)

    def reduce_granule(
        path: Path, granule_id: str, observed_at: str
    ) -> dict[tuple[int, int], TileMax]:
        exclusion, qc_note = fire_exclusion_for(
            session, token, workdir, fire_urls, granule_id, read_fire_granule
        )
        reduction = granule_reduction(
            path,
            granule_id,
            observed_at,
            fire_exclusion=exclusion,
            qc_note=qc_note,
            volcanic_sources=sources,
        )
        if raster_store is not None:
            raster.accumulate_granule(
                raster_store,
                reduction.pixels.celsius,
                reduction.pixels.lat,
                reduction.pixels.lon,
                valid=reduction.pixels.keep,
            )
        if anomaly_sink is not None and reduction.anomalies:
            merge_anomalies(anomaly_sink, reduction.anomalies)
        return reduction.tiles

    return reduce_granule


def process_granules(
    session,
    refs: list[GranuleRef],
    token: str,
    workdir: Path,
    reduce_granule: GranuleReducer | None = None,
) -> tuple[dict[tuple[int, int], TileMax], int]:
    """Download, reduce and discard granules one at a time.

    Granules are deleted immediately after processing: a full day of one
    satellite is 1.5-2.5 GB and the GitHub Actions runner disk is finite. A
    single corrupt or unavailable granule is logged and skipped rather than
    ending the run.

    ``reduce_granule`` defaults to the plain tile-only reader, which applies no
    fire mask and produces no raster; :func:`build_reducer` supplies the one
    that does both.
    """
    if reduce_granule is None:
        # Imported here so a machine without libhdf4 can still import the CLI.
        from .granule import granule_maxima as reduce_granule

    accumulator: dict[tuple[int, int], TileMax] = {}
    processed = 0

    for index, ref in enumerate(refs, start=1):
        destination = workdir / f"{index:05d}_{Path(ref.url).name}"
        try:
            download_granule(session, ref.url, destination, token)
            tiles = reduce_granule(destination, ref.granule_id, ref.observed_at)
            merge_tile_maxima(accumulator, tiles)
            processed += 1
            LOG.info(
                "granule %d/%d %s: %d tiles (running total %d)",
                index,
                len(refs),
                ref.granule_id,
                len(tiles),
                len(accumulator),
            )
        except Exception as exc:  # noqa: BLE001 - one bad granule must not end the day
            LOG.warning("granule %s failed: %s", ref.granule_id, exc)
        finally:
            destination.unlink(missing_ok=True)

    return accumulator, processed


def report_tiles(product: str, target: date, tiles: list[TileMax]) -> None:
    print(f"\n{product} ({satellite_for_product(product)}) {target.isoformat()}")
    print(f"{len(tiles)} tiles selected "
          f"(>= {HOT_TILE_THRESHOLD_C} C, plus global top {TOP_TILE_COUNT})")
    for tile in tiles[:20]:
        print(
            f"  {tile.max_c:7.2f} C  tile {tile.tile_lat:>4},{tile.tile_lon:>5}  "
            f"at {tile.max_lat:8.4f},{tile.max_lon:9.4f}  {tile.observed_at}  "
            f"{tile.granule_id}"
        )


def run_product(
    session,
    product: str,
    target: date,
    token: str,
    service_key: str | None,
    max_granules: int | None,
    dry_run: bool,
    day: DayAccumulator | None = None,
    fire_masking: bool = False,
    discovery: Discovery | None = None,
    resolver: geocode.PlaceNameResolver | None = None,
) -> ProductResult:
    """Ingest one product's day into lst_readings.

    ``day``, ``fire_masking`` and ``resolver`` are opt-in extras :func:`main`
    turns on: passing an accumulator makes this product's hot pixels and tile
    maxima accumulate into the shared state the once-per-day stages publish
    after every product has run.
    """
    discovery = discovery or Discovery()
    writer = None if dry_run else SupabaseWriter(session, service_key or "")
    run_id = None if writer is None else writer.start_run(target, product)

    granules_total = 0
    granules_processed = 0
    tiles_written = 0
    error: str | None = None

    try:
        refs = search_granules(
            session, product, target, archive=discovery.archive, bboxes=discovery.bboxes
        )
        if max_granules is not None:
            refs = refs[:max_granules]
        granules_total = len(refs)
        LOG.info(
            "%s %s: %d granules to fetch from %s",
            product,
            target.isoformat(),
            granules_total,
            discovery.holdings,
        )
        if granules_total == 0:
            raise RuntimeError(f"CMR returned no daytime {product} granules for {target}")

        fire_urls = (
            discover_fire_granules(session, product, target, discovery)
            if fire_masking
            else None
        )

        with tempfile.TemporaryDirectory(prefix="kiln-granules-") as tmp:
            workdir = Path(tmp)
            reduce_granule = None
            if fire_urls is not None or day is not None:
                reduce_granule = build_reducer(
                    session,
                    token,
                    workdir,
                    fire_urls,
                    None if day is None else day.raster,
                    None if day is None else day.anomalies.setdefault(product, {}),
                )
            accumulator, granules_processed = process_granules(
                session, refs, token, workdir, reduce_granule
            )

        if granules_processed == 0:
            raise RuntimeError(f"every {product} granule failed to download or parse")

        if day is not None:
            # Every observed tile, not just the selected ones: a tile can set an
            # all-time record without being hot enough for today's table, and
            # the all-time selection policy does its own filtering.
            day.per_product[product] = dict(accumulator)
            merge_tile_maxima(day.tiles, accumulator)

        selected = select_reported_tiles(accumulator.values())
        LOG.info(
            "%s: %d tiles observed, %d selected for storage",
            product,
            len(accumulator),
            len(selected),
        )

        # The hottest handful get a place name. The selection is decided by
        # this point, so this names exactly the rows that are about to be
        # written -- and in a dry run it records which cells that would be.
        places: dict[tuple[int, int], geocode.Place] = {}
        if resolver is not None and selected:
            places = geocode.tile_places(
                resolver, selected[: geocode.DAILY_PLACE_NAME_LIMIT]
            )

        if dry_run:
            report_tiles(product, target, selected)
            tiles_written = 0
        else:
            assert writer is not None
            tiles_written = writer.upsert_readings(selected, target, product, places)

    except Exception as exc:  # noqa: BLE001 - recorded on the run row, then reported
        error = f"{type(exc).__name__}: {exc}"
        LOG.error("%s ingestion failed: %s", product, error)

    status = resolve_run_status(granules_total, granules_processed, error)
    if writer is not None and run_id is not None:
        try:
            status = writer.finish_run(
                run_id, granules_total, granules_processed, tiles_written, error
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not mask the result
            LOG.error("could not close ingest_runs row %s: %s", run_id, exc)

    return ProductResult(
        product=product,
        status=status,
        granules_total=granules_total,
        granules_processed=granules_processed,
        tiles_written=tiles_written,
        error=error,
    )


def encode_pyramid(
    pyramid: dict[int, dict[tuple[int, int], np.ndarray]], target: date
) -> tuple[list[tuple[str, bytes]], dict[int, int]]:
    """Every tile with data, as (storage path, PNG bytes), plus per-zoom counts.

    Tiles that ended up entirely unobserved are dropped rather than uploaded as
    fully transparent images: an absent tile is how the map says "no data here".
    """
    objects: list[tuple[str, bytes]] = []
    per_zoom: dict[int, int] = {}

    for zoom in sorted(pyramid):
        count = 0
        for (tile_x, tile_y), tile in sorted(pyramid[zoom].items()):
            if not raster.tile_has_data(tile):
                continue
            objects.append(
                (
                    storage_io.tile_object_path(target, zoom, tile_x, tile_y),
                    tile_png.encode_tile_png(tile),
                )
            )
            count += 1
        per_zoom[zoom] = count

    return objects, per_zoom


def write_tiles_locally(
    objects: list[tuple[str, bytes]], manifest: dict[str, Any], tiles_dir: Path
) -> None:
    """Mirror the bucket layout on disk, so --dry-run output is servable as-is."""
    tiles_dir.mkdir(parents=True, exist_ok=True)
    for path, body in objects:
        destination = tiles_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    (tiles_dir / storage_io.MANIFEST_OBJECT).write_text(json.dumps(manifest, indent=2) + "\n")


def write_readings_locally(day: DayAccumulator, target: date, tiles_dir: Path) -> int:
    """Stage this day's readings and anomalies as JSON, for a later bulk import.

    Used when Supabase itself is unreachable (an outage, or -- as on
    2026-09-04 -- no project exists yet to write to) but the NASA download and
    every screen should run anyway rather than wait. Reuses the exact row
    builders the live writer calls, so a staged file and a live upsert payload
    are byte-for-byte the same shape; the later import step is a straight
    POST of this JSON, not a translation.

    Place names are deliberately left unresolved here: geocoding is rate
    limited to about one request a second, and running it inline would slow a
    many-day local backfill for no reason -- the existing
    ``kiln_ingest.geocode --backfill`` entry point fills names in once the
    data is actually in Supabase, exactly as it does for a live run.
    """
    day_dir = tiles_dir / target.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    rows_written = 0

    for product, tiles in day.per_product.items():
        rows = build_reading_rows(tiles.values(), target, product)
        (day_dir / f"readings_{product}.json").write_text(json.dumps(rows, indent=2) + "\n")
        rows_written += len(rows)

    for product, anomalies in day.anomalies.items():
        rows = build_anomaly_rows(anomalies.values(), target, product)
        (day_dir / f"anomalies_{product}.json").write_text(json.dumps(rows, indent=2) + "\n")
        rows_written += len(rows)

    return rows_written


def report_raster(per_zoom: dict[int, int], destination: Path) -> None:
    print(f"\nraster pyramid -> {destination}")
    for zoom in sorted(per_zoom):
        print(f"  z{zoom}: {per_zoom[zoom]:>6} tiles")
    print(f"  total: {sum(per_zoom.values())} tiles")


def publish_raster(
    store: raster.TileStore,
    target: date,
    service_key: str | None,
    dry_run: bool,
    tiles_dir: Path,
) -> bool:
    """Build, encode and publish the day's tile pyramid. True if it got out.

    Deliberately walled off from the row-based flow: by the time this runs the
    lst_readings and ingest_runs rows are already written, and a failure here
    must leave them standing. It returns False instead of raising, the caller
    exits nonzero so the workflow goes red, and the day still has its readings.
    """
    try:
        if not store:
            LOG.warning(
                "no pixels at or above %.1f C; no raster tiles for %s",
                raster.RASTER_MIN_C,
                target.isoformat(),
            )
            return True

        LOG.info(
            "raster: %d active z%d tiles holding %.0f MB",
            len(store),
            raster.MAX_ZOOM,
            raster.store_memory_mb(store),
        )
        if len(store) > raster.ACTIVE_TILE_WARN:
            LOG.warning(
                "active tile count %d is past the expected ceiling of %d; "
                "the runner may be short of memory",
                len(store),
                raster.ACTIVE_TILE_WARN,
            )

        pyramid = raster.build_pyramid(store)
        objects, per_zoom = encode_pyramid(pyramid, target)

        if dry_run:
            manifest = storage_io.build_manifest(target, tile_count=len(objects))
            write_tiles_locally(objects, manifest, tiles_dir)
            report_raster(per_zoom, tiles_dir)
            return True

        import requests  # noqa: PLC0415 - keeps import cost off --help

        uploader = storage_io.StorageUploader(requests.Session, service_key or "")
        report = uploader.upload_tiles(objects, cache_control=storage_io.TILE_CACHE_CONTROL)
        LOG.info("uploaded %d of %d tiles", report.uploaded, report.total)
        if not report.acceptable:
            raise RuntimeError(
                f"{report.failed} of {report.total} tile uploads failed, past the "
                f"{storage_io.MAX_TILE_FAILURE_RATE:.0%} tolerance"
            )

        # Last, so a reader never finds a manifest pointing at a half-built day.
        manifest = storage_io.build_manifest(target, tile_count=report.uploaded)
        uploader.upload_manifest(manifest)
        LOG.info(
            "manifest published: %s, %d tiles, z%d-%d",
            manifest["date"],
            manifest["tile_count"],
            manifest["min_zoom"],
            manifest["max_zoom"],
        )

        try:
            LOG.info("pruned %d stale tile objects", uploader.prune_old_dates())
        except Exception as exc:  # noqa: BLE001 - housekeeping never fails a good day
            LOG.warning("pruning old tile dates failed: %s", exc)

        return True

    except Exception as exc:  # noqa: BLE001 - reported through the exit code
        LOG.exception("raster stage failed: %s", exc)
        return False


def report_corroboration(screen: Corroboration, dropped_pixels: int) -> None:
    record_tier = len(screen.rejected) + len(screen.uncorroborated)
    print(f"\ncross-satellite corroboration (>= {CORROBORATION_THRESHOLD_C:.0f} C)")
    print(f"  {record_tier:>6} record-tier tiles screened")
    print(
        f"  {len(screen.rejected):>6} rejected, the other satellite disagreeing by more "
        f"than {CORROBORATION_TOLERANCE_K:.0f} K"
    )
    print(
        f"  {len(screen.uncorroborated):>6} single-satellite and uncorroborated, "
        f"routed to anomalies"
    )
    if dropped_pixels:
        print(f"  {dropped_pixels:>6} raster pixels dropped above the surviving maximum")
    for tile in sorted(screen.rejected.values(), key=lambda t: -t.max_c)[:5]:
        ceiling = screen.ceilings[(tile.tile_lat, tile.tile_lon)]
        print(
            f"    {tile.max_c:7.2f} C  tile {tile.tile_lat:>4},{tile.tile_lon:>5}  "
            f"rejected; the other satellite read {ceiling:.2f} C  {tile.granule_id}"
        )


def rewrite_screened_rows(
    session, screen: Corroboration, target: date, service_key: str
) -> int:
    """Re-upsert the daily rows whose note the corroboration screen changed.

    The daily table is written product by product, before the other satellite
    has been seen, so the screen's verdict arrives after the rows do. These are
    real observations and they stay published; this pass only corrects what
    their note says about them. The set is tiny -- record-tier tiles are rare --
    and the upsert conflict target makes it idempotent.
    """
    annotated = screen.annotated
    if not annotated:
        return 0

    by_product: dict[str, list[TileMax]] = {}
    for tile in annotated:
        product = product_from_granule_id(tile.granule_id)
        if product is None:
            LOG.warning(
                "cannot tell which product produced %s; its daily row keeps the "
                "note it was written with",
                tile.granule_id,
            )
            continue
        by_product.setdefault(product, []).append(tile)

    writer = SupabaseWriter(session, service_key)
    written = 0
    for product, tiles in sorted(by_product.items()):
        written += writer.upsert_readings(tiles, target, product)
    LOG.info("corroboration: rewrote the note on %d daily rows", written)
    return written


def collect_anomalies(
    day: DayAccumulator, screen: Corroboration
) -> dict[str, list[Anomaly]]:
    """Everything the day routed out of the weather archive, per product.

    Two sources meet here. The volcanic and wildfire rows were found granule by
    granule, as the masks removed those pixels. The two corroboration causes can
    only be known once both satellites have been reduced, which is why this runs
    where it does. Returned hottest first, which is the order a reader wants.
    """
    per_product = {product: dict(found) for product, found in day.anomalies.items()}

    for anomaly in screen.anomalies:
        product = product_from_granule_id(anomaly.tile.granule_id)
        if product is None:
            LOG.warning(
                "cannot tell which product produced %s; it gets no anomaly row",
                anomaly.tile.granule_id,
            )
            continue
        merge_anomalies(per_product.setdefault(product, {}), {anomaly.key: anomaly})

    return {
        product: sorted(found.values(), key=lambda a: (-a.tile.max_c, a.cause))
        for product, found in sorted(per_product.items())
        if found
    }


def report_anomalies(per_product: dict[str, list[Anomaly]], target: date) -> None:
    """Print the rows a real run would upsert, and upsert nothing."""
    total = sum(len(found) for found in per_product.values())
    print(f"\nanomalies (dry run, nothing written) {target.isoformat()}")
    print(
        f"  volcanic >= {VOLCANIC_ANOMALY_MIN_C:.0f} C, wildfire >= "
        f"{WILDFIRE_ANOMALY_MIN_C:.0f} C, both corroboration causes >= "
        f"{CORROBORATION_THRESHOLD_C:.0f} C"
    )
    if not total:
        print("  no non-weather readings to route out of the weather archive")
        return

    print(f"  {total} row(s) would be upserted into kiln.{ANOMALIES_TABLE}")
    for product, anomalies in per_product.items():
        for anomaly in anomalies:
            row = build_anomaly_row(anomaly, target, product)
            print(
                f"  {row['max_c']:7.2f} C  tile {row['tile_lat']:>4},{row['tile_lon']:>5}  "
                f"at {row['max_lat']:8.4f},{row['max_lon']:9.4f}  "
                f"{row['cause']:<20} {row['source_slug'] or '-':<14} "
                f"{row['product']}  {row['granule_id']}"
            )


def write_anomalies(
    session,
    per_product: dict[str, list[Anomaly]],
    target: date,
    service_key: str,
    resolver: geocode.PlaceNameResolver | None = None,
) -> int:
    """Upsert the day's anomaly rows, product by product."""
    if not per_product:
        return 0

    writer = SupabaseWriter(session, service_key)
    written = 0
    for product, anomalies in per_product.items():
        places = (
            {} if resolver is None else geocode.anomaly_places(resolver, anomalies)
        )
        written += writer.upsert_anomalies(anomalies, target, product, places)
    LOG.info("anomalies: wrote %d row(s) for %s", written, target.isoformat())
    return written


def screen_day(
    day: DayAccumulator,
    target: date,
    service_key: str | None,
    dry_run: bool,
    resolver: geocode.PlaceNameResolver | None = None,
) -> Corroboration:
    """Run the cross-satellite screen and let its verdict govern the day.

    This sits between the per-product loop and both once-per-day stages, which
    is the only place it can sit: it needs both satellites to have been seen,
    and the archive it protects merges by maximum and never forgets.

    ``day.tiles`` comes out of here holding corroborated weather and nothing
    else. Rejected readings and record-tier readings nothing witnessed are
    routed to the anomalies section, and their raster pixels are dropped with
    them, so the map and the archive tell the same story.
    """
    screen = corroborate_day(day.per_product)

    dropped = raster.drop_above_ceilings(day.raster, screen.ceilings)
    day.tiles = screen.tiles
    anomalies = collect_anomalies(day, screen)

    if screen.rejected:
        LOG.warning(
            "corroboration rejected %d record-tier tile(s) for %s; they stay in "
            "lst_readings and are barred from the archive",
            len(screen.rejected),
            target.isoformat(),
        )
    for key, tile in sorted(screen.rejected.items()):
        LOG.warning(
            "  tile %s: %.2f C from %s contradicted by %.2f C from the other satellite",
            key,
            tile.max_c,
            tile.granule_id,
            screen.ceilings[key],
        )
    if screen.uncorroborated:
        LOG.info(
            "corroboration: %d record-tier tile(s) had no second satellite; they are "
            "published as anomalies rather than as weather",
            len(screen.uncorroborated),
        )
    if dropped:
        LOG.info("corroboration: dropped %d raster pixels above a surviving maximum", dropped)
    for product, found in anomalies.items():
        LOG.info("anomalies: %s contributed %d row(s)", product, len(found))

    if dry_run:
        # Resolve nothing, but let the resolver record which cells a real run
        # would ask about, so the dry run's place-name report is complete.
        if resolver is not None:
            for found in anomalies.values():
                geocode.anomaly_places(resolver, found)
        report_corroboration(screen, dropped)
        report_anomalies(anomalies, target)
    elif screen.annotated or anomalies:
        import requests  # noqa: PLC0415 - keeps import cost off --help

        with requests.Session() as session:
            if screen.annotated:
                rewrite_screened_rows(session, screen, target, service_key or "")
            write_anomalies(session, anomalies, target, service_key or "", resolver)

    return screen


def report_alltime(day: DayAccumulator, target: date) -> None:
    print(f"\nall-time archive (dry run, nothing uploaded) through {target.isoformat()}")
    print(f"  {len(day.raster):>6} base-zoom tiles would be merged into alltime-state/")
    print(f"  {len(day.tiles):>6} one-degree tiles are candidates for the all-time table")
    print("  stored state was not read, so which of them would actually set a")
    print("  record is not knowable from a dry run")


def read_alltime_state(
    uploader: Any, keys: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], "np.ndarray | None"]:
    """The stored all-time state for the tiles today touched.

    An unreadable state object is warned about loudly and treated as absent,
    which rebuilds that one tile's history from today. It should never happen;
    if it does, the warning names the tile so the loss is visible rather than
    silent.
    """
    paths = {key: storage_io.alltime_state_path(key[0], key[1]) for key in keys}
    bodies = uploader.download_objects([paths[key] for key in keys])

    stored: dict[tuple[int, int], "np.ndarray | None"] = {}
    for key in keys:
        body = bodies.get(paths[key])
        if body is None:
            stored[key] = None
            continue
        try:
            stored[key] = alltime.load_state(body)
        except alltime.CorruptStateError as exc:
            LOG.warning(
                "all-time state %s is unreadable (%s); rebuilding that tile's history "
                "from today, so its record before today is lost",
                paths[key],
                exc,
            )
            stored[key] = None
    return stored


def alltime_level_reader(uploader: Any):
    """A :data:`kiln_ingest.alltime.FetchLevel` backed by the published pyramid."""

    def fetch_level(zoom: int, keys: Sequence[tuple[int, int]]):
        paths = {key: storage_io.alltime_tile_path(zoom, key[0], key[1]) for key in keys}
        bodies = uploader.download_objects([paths[key] for key in keys])

        published: dict[tuple[int, int], "np.ndarray | None"] = {}
        for key in keys:
            body = bodies.get(paths[key])
            if body is None:
                published[key] = None
                continue
            try:
                published[key] = tile_png.decode_indices_png(body)
            except Exception as exc:  # noqa: BLE001 - a bad tile is rebuilt, not fatal
                LOG.warning(
                    "published all-time tile %s is unreadable (%s); rebuilding it from "
                    "today alone, which drops the parts of it no other day touched",
                    paths[key],
                    exc,
                )
                published[key] = None
        return published

    return fetch_level


def upsert_alltime_rows(
    session,
    day: DayAccumulator,
    target: date,
    service_key: str,
    resolver: geocode.PlaceNameResolver | None = None,
) -> int:
    """Rows for tiles whose all-time record today improved."""
    writer = SupabaseWriter(session, service_key)
    existing = writer.fetch_alltime_maxima()
    improved = alltime.select_alltime_upserts(existing, day.tiles)

    # Only the tiles actually being written: an all-time row is the most read
    # thing on the site, so these are the names most worth spending on.
    places = (
        {} if resolver is None else geocode.tile_places(resolver, improved)
    )

    rows = []
    for tile in improved:
        product = product_from_granule_id(tile.granule_id)
        if product is None:
            LOG.warning(
                "cannot tell which product produced %s; skipping its all-time row",
                tile.granule_id,
            )
            continue
        place = places.get(tile.key, geocode.Place())
        rows.append(
            build_alltime_row(
                tile,
                target,
                product,
                place_name=place.name,
                country=place.country,
            )
        )

    LOG.info(
        "all-time table: %d tiles known, %d improved today, %d rows written",
        len(existing),
        len(improved),
        len(rows),
    )
    return writer.upsert_alltime(rows)


def publish_alltime(
    day: DayAccumulator,
    target: date,
    service_key: str | None,
    dry_run: bool,
    resolver: geocode.PlaceNameResolver | None = None,
) -> bool:
    """Fold the day into the permanent all-time archive. True if it got out.

    Everything merged here comes out of ``day``, which is filled only from
    fire-masked and plausibility-screened pixels. That ordering is the whole
    correctness story: a merge is a maximum, so one fire pixel admitted once is
    an all-time record no later day can undo.

    The upload order -- display tiles, then state, then rows, then manifest --
    is chosen so that re-running the date after any failure repairs it. State is
    written last of the two because it is what decides whether a tile counts as
    changed: advancing it before the tiles were safely up would make the next
    run see nothing to do and leave the pyramid permanently stale.
    """
    try:
        if not day.raster and not day.tiles:
            LOG.warning("nothing observed for %s; all-time archive untouched", target)
            return True

        if dry_run:
            report_alltime(day, target)
            return True

        import requests  # noqa: PLC0415 - keeps import cost off --help

        uploader = storage_io.StorageUploader(requests.Session, service_key or "")
        prior = uploader.read_manifest(storage_io.ALLTIME_MANIFEST_OBJECT)

        keys = sorted(day.raster)
        stored = read_alltime_state(uploader, keys)
        changed = alltime.merge_day(day.raster, stored)
        created = sum(1 for key in changed if stored.get(key) is None)
        LOG.info(
            "all-time archive: %d tiles touched, %d improved (%d of them new)",
            len(keys),
            len(changed),
            created,
        )

        if changed:
            levels, created_parents = alltime.build_alltime_levels(
                changed, alltime_level_reader(uploader)
            )
            created += len(created_parents)

            objects = [
                (
                    storage_io.alltime_tile_path(zoom, key[0], key[1]),
                    tile_png.encode_indices_png(ranks),
                )
                for zoom in sorted(levels)
                for key, ranks in sorted(levels[zoom].items())
                if alltime.level_has_data(ranks)
            ]
            report = uploader.upload_tiles(objects)
            LOG.info("all-time pyramid: uploaded %d of %d tiles", report.uploaded, report.total)
            if not report.acceptable:
                raise RuntimeError(
                    f"{report.failed} of {report.total} all-time tile uploads failed, past "
                    f"the {storage_io.MAX_TILE_FAILURE_RATE:.0%} tolerance"
                )

            # No tolerance here, unlike the display tiles: a state object that
            # does not land loses that tile's improvement for good, and failing
            # the run means someone re-runs the date, which merges to exactly
            # the same answer.
            state_objects = [
                (storage_io.alltime_state_path(key[0], key[1]), alltime.dump_state(state))
                for key, state in sorted(changed.items())
            ]
            state_report = uploader.upload_tiles(
                state_objects, content_type=storage_io.STATE_CONTENT_TYPE
            )
            if state_report.failed:
                raise RuntimeError(
                    f"{state_report.failed} of {state_report.total} all-time state objects "
                    "failed to upload; re-run this date to merge them again"
                )
            LOG.info("all-time state: %d tiles written", state_report.uploaded)

        # Runs even when no raster tile changed: the raster holds only pixels at
        # or above the display threshold, so a cooler tile can still set a
        # record the table should carry.
        with requests.Session() as session:
            upsert_alltime_rows(session, day, target, service_key or "", resolver)

        manifest = storage_io.build_alltime_manifest(
            since=alltime.alltime_since(prior, target),
            through=target,
            tile_count=alltime.alltime_tile_total(prior, created),
        )
        uploader.upload_manifest(manifest, storage_io.ALLTIME_MANIFEST_OBJECT)
        LOG.info(
            "all-time manifest published: %s through %s, %d tiles",
            manifest["since"],
            manifest["through"],
            manifest["tile_count"],
        )
        return True

    except Exception as exc:  # noqa: BLE001 - reported through the exit code
        LOG.exception("all-time stage failed: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    target = args.date or yesterday_utc()
    products = [args.product] if args.product else sorted(PRODUCTS)
    discovery = Discovery(archive=args.archive, bboxes=tuple(args.bbox or ()))
    LOG.info(
        "%s: searching %s%s",
        target.isoformat(),
        discovery.holdings,
        f" within {len(discovery.bboxes)} bounding box(es)" if discovery.bboxes else "",
    )

    token = os.environ.get("EARTHDATA_TOKEN", "")
    if not token:
        LOG.error("EARTHDATA_TOKEN is not set; granule downloads require a bearer token")
        return 2
    if not check_earthdata_token(token, datetime.now(timezone.utc)):
        return 2

    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not args.dry_run and not service_key:
        LOG.error("SUPABASE_SERVICE_KEY is not set; use --dry-run to skip Supabase writes")
        return 2

    import requests  # noqa: PLC0415 - keeps import cost off --help

    # One accumulator shared across products: the daily pyramid and the
    # all-time archive are per day, not per satellite, and every write into
    # either is a maximum.
    day = DayAccumulator()

    # One resolver for the whole run, so a cell wanted by both an all-time row
    # and a daily row costs one cache read and at most one request. A dry run
    # keeps the service key when it has one: reading the cache writes nothing
    # and is what makes its report say which cells are genuinely new rather
    # than listing every cell the day touched.
    resolver = geocode.PlaceNameResolver(
        service_key=service_key, dry_run=args.dry_run
    )

    results: list[ProductResult] = []
    with requests.Session() as session:
        for product in products:
            try:
                results.append(
                    run_product(
                        session,
                        product,
                        target,
                        token,
                        service_key,
                        args.max_granules,
                        args.dry_run,
                        day=day,
                        fire_masking=True,
                        discovery=discovery,
                        resolver=resolver,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - e.g. Supabase down before the run row exists
                LOG.exception("%s ingestion raised before it could be recorded", product)
                results.append(
                    ProductResult(
                        product=product,
                        status=STATUS_FAILED,
                        granules_total=0,
                        granules_processed=0,
                        tiles_written=0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

    # Between the loop and the stages: the screen needs both satellites, and
    # everything after it writes to surfaces that are hard or impossible to undo.
    screen_day(day, target, service_key, args.dry_run, resolver)

    # Archive-mode runs are historical backfill: they feed the ALL-TIME surfaces
    # only. Publishing the daily pyramid/manifest for a 2007 date would replace
    # the live site's "most recent" view with history and let per-date pruning
    # eat the real current pyramid (this happened; see session doc 2026-09-01).
    if discovery.archive and not args.dry_run:
        LOG.info("archive mode: skipping the daily raster/manifest publish for %s", target)
        raster_ok = True
    else:
        raster_ok = publish_raster(
            day.raster, target, service_key, args.dry_run, args.tiles_dir
        )
    alltime_ok = publish_alltime(day, target, service_key, args.dry_run, resolver)

    if args.dry_run:
        geocode.report_pending(resolver)
        if args.tiles_dir:
            staged = write_readings_locally(day, target, args.tiles_dir)
            LOG.info("staged %d reading/anomaly row(s) locally at %s", staged, args.tiles_dir)
    elif resolver.requests_made:
        LOG.info("place names: %d cell(s) reverse geocoded", resolver.requests_made)

    for result in results:
        LOG.info(
            "%s: %s (%d/%d granules, %d tiles written)",
            result.product,
            result.status,
            result.granules_processed,
            result.granules_total,
            result.tiles_written,
        )

    if not raster_ok:
        LOG.error(
            "raster tiles were not published for %s; the lst_readings rows for "
            "that date stand and can be re-rastered by re-running the date",
            target.isoformat(),
        )
    if not alltime_ok:
        LOG.error(
            "the all-time archive was not updated for %s; it is unchanged rather "
            "than half-written, and re-running the date merges it again",
            target.isoformat(),
        )

    return 0 if any(r.ok for r in results) and raster_ok and alltime_ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
