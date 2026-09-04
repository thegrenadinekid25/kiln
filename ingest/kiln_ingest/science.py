"""Pure science core for the Kiln land-surface-temperature pipeline.

Everything in this module operates on plain numpy arrays, mappings and Python
scalars. Nothing here touches the network or imports pyhdf, so the entire
scientific path -- scaling, QC masking, geolocation, per-tile aggregation,
threshold/top-N selection -- is testable with synthetic arrays. The one file it
reads is its own bundled volcano list, and every function that uses it also
accepts the sources directly, so no test needs the file to exist.

Reference: MODIS Land Surface Temperature products MOD11_L2 (Terra) and
MYD11_L2 (Aqua), collection 6.1.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Collection, Iterable, Mapping, Sequence

import numpy as np

LOG = logging.getLogger(__name__)

# --- Documented product constants ---------------------------------------------------

# MOD11/MYD11 store LST as uint16 with a fixed 0.02 K scale factor. We read the
# real value out of the SDS attributes and check it against this, rather than
# multiplying blindly: a collection change that altered the scaling would
# otherwise silently produce temperatures off by orders of magnitude.
EXPECTED_LST_SCALE_FACTOR = 0.02
DEFAULT_LST_FILL = 0
KELVIN_ZERO_C = 273.15

# Plausibility band applied after scaling. Real land-surface temperatures on
# Earth live far inside this; anything outside means the granule is corrupt or
# the scaling assumption broke, and we would rather drop the pixel than publish
# a nonsense record.
PHYSICAL_MIN_C = -150.0
PHYSICAL_MAX_C = 200.0

# Selection policy for what reaches the database.
HOT_TILE_THRESHOLD_C = 40.0
TOP_TILE_COUNT = 10

# Human-readable record of the QC policy applied, stored on every row.
QC_NOTE = "mandatory QA 00/01; LST error flag <= 2K"

# Appended to a tile's note when the active-fire mask threw away at least one
# otherwise-valid pixel inside it: the published maximum is the hottest
# unburnt pixel, not the hottest pixel.
FIRE_MASKED_NOTE = "; fire-masked"

# Appended when the matching MOD14/MYD14 granule could not be fetched or read.
# The temperature is still a real measurement; it just has not been checked
# against fire detections, and saying so is better than quietly implying it has.
FIRE_UNAVAILABLE_NOTE = "; fire mask unavailable"

# Appended when the high-latitude plausibility screen dropped a pixel in a tile.
HIGH_LATITUDE_OUTLIER_NOTE = "; high-latitude outlier excluded"

# Appended to a reading the other satellite contradicted. The reading is real
# and still published in the daily table; it is barred from the permanent
# archive, where a maximum can never be walked back.
CORROBORATION_REJECTED_NOTE = "; rejected by cross-satellite corroboration"

# Appended to an extreme reading no second satellite saw that day.
UNCORROBORATED_NOTE = "; single-satellite, uncorroborated"

# Appended when a weather tile's maximum was taken after volcanic pixels were
# removed from it: the published number is the hottest ordinary ground in the
# tile, not the hottest ground.
VOLCANIC_MASKED_NOTE = "; volcanic source excluded"

# Notes carried by the anomaly rows themselves, which say what the reading is
# rather than what was taken out of it. Shown verbatim next to the number.
VOLCANIC_ANOMALY_NOTE = "; volcanic source, not weather"
ACTIVE_FIRE_ANOMALY_NOTE = "; active fire, not weather"

# The four reasons a reading is published as an anomaly instead of as weather.
# These strings are the CHECK constraint on kiln.anomaly_readings.cause and are
# shown to readers as written, so they are worded, not coded.
CAUSE_VOLCANIC = "volcanic"
CAUSE_WILDFIRE = "wildfire"
CAUSE_FAILED_CORROBORATION = "failed corroboration"
CAUSE_UNCORROBORATED = "uncorroborated"
ANOMALY_CAUSES = (
    CAUSE_VOLCANIC,
    CAUSE_WILDFIRE,
    CAUSE_FAILED_CORROBORATION,
    CAUSE_UNCORROBORATED,
)

# 1-degree tile grid bounds, matching the smallint CHECK constraints on
# kiln.lst_readings (tile_lat -90..89, tile_lon -180..179).
TILE_LAT_MIN, TILE_LAT_MAX = -90, 89
TILE_LON_MIN, TILE_LON_MAX = -180, 179


class UnexpectedGranuleError(ValueError):
    """A granule's metadata does not match the documented MOD11/MYD11 layout."""


# --- Scaling ------------------------------------------------------------------------


@dataclass(frozen=True)
class LstScaling:
    scale_factor: float
    add_offset: float
    fill_value: int


def resolve_lst_scaling(attrs: Mapping[str, object]) -> LstScaling:
    """Pull scaling metadata off the LST SDS attributes, refusing surprises.

    A missing or unexpected ``scale_factor`` is fatal: it is the single value
    that turns stored integers into kelvin, and guessing it wrong is worse than
    not running at all.
    """
    if "scale_factor" not in attrs:
        raise UnexpectedGranuleError("LST SDS has no scale_factor attribute")

    scale_factor = float(attrs["scale_factor"])  # type: ignore[arg-type]
    if not math.isclose(scale_factor, EXPECTED_LST_SCALE_FACTOR, rel_tol=1e-6):
        raise UnexpectedGranuleError(
            f"LST scale_factor is {scale_factor}, expected "
            f"{EXPECTED_LST_SCALE_FACTOR}; refusing to guess the units"
        )

    add_offset = float(attrs.get("add_offset", 0.0))  # type: ignore[arg-type]
    fill_value = int(attrs.get("_FillValue", DEFAULT_LST_FILL))  # type: ignore[arg-type]
    return LstScaling(scale_factor=scale_factor, add_offset=add_offset, fill_value=fill_value)


def decode_lst_celsius(
    raw_lst: np.ndarray, scaling: LstScaling
) -> tuple[np.ndarray, np.ndarray]:
    """Convert raw stored LST counts to Celsius plus a validity mask.

    Returns ``(celsius, valid)`` where ``valid`` is False for fill pixels and
    for anything outside the physical plausibility band.
    """
    raw = np.asarray(raw_lst)
    kelvin = raw.astype(np.float64) * scaling.scale_factor + scaling.add_offset
    celsius = kelvin - KELVIN_ZERO_C

    valid = raw != scaling.fill_value
    valid &= np.isfinite(celsius)
    valid &= (celsius >= PHYSICAL_MIN_C) & (celsius <= PHYSICAL_MAX_C)
    return celsius, valid


# --- Quality control ----------------------------------------------------------------


def qc_keep_mask(qc: np.ndarray, max_error_class: int = 1) -> np.ndarray:
    """Pixels whose QC byte clears the Kiln quality bar.

    MOD11/MYD11 QC bit layout:

    * bits 0-1 mandatory QA: 00 produced good quality, 01 produced other
      quality, 10 not produced (cloud), 11 not produced (other).
    * bits 6-7 LST error flag: 00 average error <= 1K, 01 <= 2K, 10 <= 3K,
      11 > 3K.

    We keep a pixel when mandatory QA says the LST was actually produced
    (00 or 01) and the error flag is at or below ``max_error_class`` -- 1 by
    default, i.e. average error <= 2K.
    """
    qc_bytes = np.asarray(qc).astype(np.uint8)
    mandatory = qc_bytes & 0b11
    error_class = (qc_bytes >> 6) & 0b11
    return (mandatory <= 1) & (error_class <= max_error_class)


# --- Latitude plausibility screen ---------------------------------------------------

# The fire mask catches what MOD14/MYD14 detected. It cannot catch a fire below
# the detector's threshold -- a subpixel flame front, a smouldering peat field --
# and one of those put a 78.75 C reading at 64.96 N in Siberia on the map.
#
# This is the backstop, and it is deliberately a single conservative band rather
# than a latitude-dependent curve:
#
# * The verified global maximum land-surface temperature is 80.8 C, at about
#   31 N (Lut Desert and Sonoran Desert, Zhao et al. 2021).
# * The Turpan Depression at 42.9 N legitimately exceeds 65 C.
# * Poleward of 50 degrees, no verified LST above 60 C has ever been observed:
#   the peak insolation and land cover that produce 60 C+ ground do not occur
#   there. Anything reading above it is a fire or an artifact.
#
# So the screen fires only on the combination of both extremes. A tighter band
# at lower latitudes would clip real records, which is a far worse error than
# leaving a rare high-latitude artifact in: this map's whole claim is that its
# numbers are real measurements.
HIGH_LATITUDE_DEGREES = 50.0
HIGH_LATITUDE_MAX_C = 60.0


def plausibility_keep_mask(celsius: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Pixels that survive the latitude plausibility screen.

    True keeps the pixel. Only pixels that are both poleward of
    :data:`HIGH_LATITUDE_DEGREES` and hotter than :data:`HIGH_LATITUDE_MAX_C`
    are rejected; everything else passes untouched, at every latitude.
    """
    celsius = np.asarray(celsius, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    implausible = (np.abs(lat) > HIGH_LATITUDE_DEGREES) & (celsius > HIGH_LATITUDE_MAX_C)
    return ~implausible


# --- Geolocation --------------------------------------------------------------------


def nearest_source_indices(target_length: int, source_length: int) -> np.ndarray:
    """Nearest-neighbour index map from a fine axis onto a coarse one."""
    if source_length < 1 or target_length < 1:
        raise UnexpectedGranuleError("geolocation and LST arrays must be non-empty")
    positions = (np.arange(target_length) + 0.5) * source_length / target_length - 0.5
    return np.clip(np.rint(positions).astype(np.intp), 0, source_length - 1)


def expand_geolocation(
    lat_sub: np.ndarray, lon_sub: np.ndarray, target_shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Map each 1km LST pixel to the nearest subsampled geolocation point.

    MOD11_L2/MYD11_L2 embed Latitude and Longitude SDSs subsampled 5x in both
    directions relative to the 1km LST grid. Proper reconstruction would
    interpolate (and account for the bow-tie effect at swath edges); we instead
    take the nearest stored point. The approximation is bounded by roughly half
    the subsampling stride -- a couple of kilometres -- which is far below the
    ~111 km granularity of the 1-degree tiles this feeds, so it cannot move a
    pixel into the wrong tile except within a few km of a tile boundary. Tiles
    are a display convenience, not a measurement, so that is acceptable.
    """
    lat_sub = np.asarray(lat_sub, dtype=np.float64)
    lon_sub = np.asarray(lon_sub, dtype=np.float64)
    if lat_sub.shape != lon_sub.shape:
        raise UnexpectedGranuleError(
            f"Latitude shape {lat_sub.shape} does not match Longitude {lon_sub.shape}"
        )
    if lat_sub.ndim != 2:
        raise UnexpectedGranuleError("geolocation SDSs must be 2-dimensional")

    rows = nearest_source_indices(target_shape[0], lat_sub.shape[0])
    cols = nearest_source_indices(target_shape[1], lat_sub.shape[1])
    return lat_sub[np.ix_(rows, cols)], lon_sub[np.ix_(rows, cols)]


def geolocation_valid(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Mask of coordinates that are finite and on the globe.

    MODIS geolocation uses -999.0 for unlocated pixels.
    """
    lat = np.asarray(lat)
    lon = np.asarray(lon)
    return (
        np.isfinite(lat)
        & np.isfinite(lon)
        & (lat >= -90.0)
        & (lat <= 90.0)
        & (lon >= -180.0)
        & (lon <= 180.0)
    )


def tile_indices(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """1-degree tile coordinates: floor of lat/lon, clipped to the table's range."""
    tile_lat = np.clip(np.floor(np.asarray(lat)), TILE_LAT_MIN, TILE_LAT_MAX)
    tile_lon = np.clip(np.floor(np.asarray(lon)), TILE_LON_MIN, TILE_LON_MAX)
    return tile_lat.astype(np.int64), tile_lon.astype(np.int64)


def tiles_covering(
    lat: np.ndarray, lon: np.ndarray, selected: np.ndarray
) -> frozenset[tuple[int, int]]:
    """The 1-degree tiles holding the selected pixels.

    Used to record which tiles an exclusion touched, so their rows can say what
    was taken out of them.
    """
    tile_lat, tile_lon = tile_indices(lat[selected], lon[selected])
    return frozenset(zip(tile_lat.tolist(), tile_lon.tolist()))


# --- Volcanic sources ---------------------------------------------------------------

# A lava lake is real ground at a real temperature, and it is not weather. The
# all-time archive's top entry was 90.37 C at 13.59 N 40.67 E -- Erta Ale's lava
# lake. The cross-satellite corroboration screen passes it, and passes it
# correctly: both satellites see the lake hot on every overpass, which is
# exactly what makes it not a transient artifact. That screen catches things
# that happen once. A lava lake happens always, so only a named list of vents
# can tell it apart from desert.
#
# The screen therefore runs off a curated file rather than a heuristic. Each
# vent carries its own radius because the thing being excluded differs in size:
# a single summit crater is a couple of kilometres across, an active shield
# volcano's flow field is tens.
VOLCANIC_SOURCES_FILE = Path(__file__).resolve().parent / "data" / "volcanic_sources.json"

DEFAULT_VENT_RADIUS_KM = 7.0

# Below this, a pixel near a vent is ordinary sun-warmed ground that happens to
# sit on a volcano, not the vent. Publishing it as a volcanic anomaly would be
# as wrong as publishing it as a weather record.
VOLCANIC_ANOMALY_MIN_C = 50.0

# One degree of latitude, and of longitude at the equator. Distances here are
# compared against radii of a few kilometres, where treating the local
# neighbourhood as flat costs metres.
KM_PER_DEGREE = 111.195


@dataclass(frozen=True)
class VolcanicSource:
    """One curated vent: where it is, how far its heat reaches, who says so."""

    slug: str
    name: str
    country: str
    lat: float
    lon: float
    radius_km: float = DEFAULT_VENT_RADIUS_KM
    source_name: str = ""
    source_url: str = ""
    notes: str | None = None


_VOLCANIC_CACHE: dict[Path, tuple[VolcanicSource, ...]] = {}


def parse_volcanic_sources(entries: Iterable[Mapping[str, object]]) -> tuple[VolcanicSource, ...]:
    """Curated list entries as :class:`VolcanicSource` records.

    An entry missing a coordinate or a slug is dropped with a warning rather
    than raising: one malformed line in the list must not take the whole screen
    down, and a screen that silently ran with no vents at all would be worse
    still, which is why the count is logged either way.
    """
    sources: list[VolcanicSource] = []
    for entry in entries:
        try:
            sources.append(
                VolcanicSource(
                    slug=str(entry["slug"]),
                    name=str(entry["name"]),
                    country=str(entry.get("country", "")),
                    lat=float(entry["lat"]),  # type: ignore[arg-type]
                    lon=float(entry["lon"]),  # type: ignore[arg-type]
                    radius_km=float(entry.get("radius_km", DEFAULT_VENT_RADIUS_KM)),  # type: ignore[arg-type]
                    source_name=str(entry.get("source_name", "")),
                    source_url=str(entry.get("source_url", "")),
                    notes=None if entry.get("notes") is None else str(entry["notes"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            LOG.warning("skipping malformed volcanic source entry %r: %s", entry, exc)
    return tuple(sources)


def load_volcanic_sources(path: Path | str | None = None) -> tuple[VolcanicSource, ...]:
    """The curated vent list, read once per path and then remembered.

    A missing or unreadable list is warned about loudly and treated as empty.
    That is the wrong answer -- it lets a lava lake back into the weather
    archive -- but refusing to ingest the day would be worse, and the warning
    names the file so the gap is visible rather than silent.
    """
    resolved = Path(path) if path is not None else VOLCANIC_SOURCES_FILE
    cached = _VOLCANIC_CACHE.get(resolved)
    if cached is not None:
        return cached

    try:
        entries = json.loads(resolved.read_text())
    except (OSError, ValueError) as exc:
        LOG.warning(
            "volcanic source list %s could not be read (%s); no vent will be "
            "excluded from the weather archive on this run",
            resolved,
            exc,
        )
        _VOLCANIC_CACHE[resolved] = ()
        return ()

    if not isinstance(entries, list):
        LOG.warning("volcanic source list %s is not a JSON array; ignoring it", resolved)
        _VOLCANIC_CACHE[resolved] = ()
        return ()

    sources = parse_volcanic_sources(entries)
    LOG.info("volcanic screen: %d vent(s) loaded from %s", len(sources), resolved)
    _VOLCANIC_CACHE[resolved] = sources
    return sources


def resolve_volcanic_sources(
    sources: Sequence[VolcanicSource] | None,
) -> tuple[VolcanicSource, ...]:
    """``None`` means the bundled list; an explicit sequence is taken as given.

    The default is fail-closed on purpose: a caller that says nothing about
    volcanoes gets the screen, and only a caller that passes an empty sequence
    opts out of it.
    """
    return load_volcanic_sources() if sources is None else tuple(sources)


def _vent_candidates(
    lat: np.ndarray, lon: np.ndarray, sources: Sequence[VolcanicSource]
) -> list[tuple[int, VolcanicSource]]:
    """The vents worth measuring against, paired with their index in ``sources``.

    A per-axis bounding-box test run once, so the distance calculation -- which
    allocates a full granule's worth of temporaries per vent -- is skipped for
    the vents nowhere near this swath. The list is global and a MODIS granule
    covers a couple of thousand kilometres, so this is nearly always all but a
    handful of them, and it takes the screen from over a second per granule to
    a few milliseconds.

    Deliberately generous. A rectangle drawn around each circle can only admit
    a vent the distance test then rejects; it can never exclude one that test
    would have kept. Coordinates spanning the antimeridian produce a longitude
    range covering the globe, which excludes nothing -- the conservative answer.
    """
    if not sources:
        return []

    on_globe = geolocation_valid(lat, lon)
    if not on_globe.any():
        return []

    lat_values = np.asarray(lat, dtype=np.float64)[on_globe]
    lon_values = np.asarray(lon, dtype=np.float64)[on_globe]
    lat_min, lat_max = float(lat_values.min()), float(lat_values.max())
    lon_min, lon_max = float(lon_values.min()), float(lon_values.max())

    candidates: list[tuple[int, VolcanicSource]] = []
    for index, source in enumerate(sources):
        margin_lat = float(source.radius_km) / KM_PER_DEGREE
        if not lat_min - margin_lat <= source.lat <= lat_max + margin_lat:
            continue

        # A degree of longitude shrinks toward the poles, so the same radius
        # spans more of them. At the pole itself it spans all of them.
        scale = math.cos(math.radians(source.lat))
        margin_lon = 180.0 if scale <= 0.0 else min(180.0, margin_lat / scale)

        # Compared as a wrapped distance from the middle of the longitude
        # range, so a vent on the far side of the antimeridian from a swath
        # beside it is still measured. A swath that itself crosses the
        # antimeridian has a range spanning the globe, which excludes nothing.
        half_width = (lon_max - lon_min) / 2.0
        centre = (lon_min + lon_max) / 2.0
        offset = abs((source.lon - centre + 180.0) % 360.0 - 180.0)
        if offset > half_width + margin_lon:
            continue

        candidates.append((index, source))

    return candidates


def vents_in_range(
    lat: np.ndarray, lon: np.ndarray, sources: Sequence[VolcanicSource]
) -> tuple[VolcanicSource, ...]:
    """The vents whose radius could reach anywhere in these coordinates."""
    return tuple(source for _, source in _vent_candidates(lat, lon, sources))


def volcanic_vent_indices(
    lat: np.ndarray, lon: np.ndarray, sources: Sequence[VolcanicSource]
) -> np.ndarray:
    """Index of the nearest vent covering each coordinate, -1 where none does.

    Distance is equirectangular, taken at the vent's latitude. Over the few
    kilometres a radius spans, the error against a great-circle distance is
    centimetres, and the vent radius is itself an approximation of an irregular
    hot area -- so a more exact formula would be false precision.

    Longitude differences are wrapped into -180..180, so a vent sitting beside
    the antimeridian covers the ground on both sides of it.
    """
    lat_array = np.asarray(lat, dtype=np.float64)
    lon_array = np.asarray(lon, dtype=np.float64)
    nearest = np.full(lat_array.shape, -1, dtype=np.int64)

    candidates = _vent_candidates(lat_array, lon_array, sources)
    if not candidates:
        return nearest

    best = np.full(lat_array.shape, np.inf, dtype=np.float64)
    for index, source in candidates:
        north_km = (lat_array - source.lat) * KM_PER_DEGREE
        east_degrees = (lon_array - source.lon + 180.0) % 360.0 - 180.0
        east_km = east_degrees * KM_PER_DEGREE * math.cos(math.radians(source.lat))
        distance = np.hypot(north_km, east_km)

        # <= so a pixel exactly on the radius is inside it: the radius is the
        # edge of the vent's heat, not the first point outside it.
        covered = (distance <= float(source.radius_km)) & (distance < best)
        best = np.where(covered, distance, best)
        nearest = np.where(covered, index, nearest)

    return nearest


def volcanic_excluded_mask(
    lat: np.ndarray, lon: np.ndarray, sources: Sequence[VolcanicSource]
) -> np.ndarray:
    """Pixels inside some vent's radius."""
    return volcanic_vent_indices(lat, lon, sources) >= 0


def nearest_vent(
    lat: float, lon: float, sources: Sequence[VolcanicSource]
) -> VolcanicSource | None:
    """The vent covering one coordinate, or None. Nearest wins where two overlap."""
    index = int(volcanic_vent_indices(np.array([lat]), np.array([lon]), sources)[0])
    return sources[index] if index >= 0 else None


# --- Active fire masking ------------------------------------------------------------

# A burning pixel is a real land-surface temperature, but it is not the climate
# signal Kiln publishes: a 300 C flame front would top the map every day of fire
# season and say nothing about how hot the ground is. MOD14/MYD14 flag the fire
# pixels of the same overpass, and we drop them plus a guard ring, because the
# 1 km LST footprint next to a fire is contaminated by it.

# 0.02 degrees is roughly 2.2 km of latitude, so a fire bin plus its eight
# neighbours excludes a ring 2-6 km wide depending on latitude and where in the
# bin the fire sits.
FIRE_BIN_DEGREES = 0.02

# Every fire-excluded pixel leaves the weather path; this is the separate
# question of which ones are worth publishing as anomalies. A burning pixel at
# 45 C is a grass fire the mask caught doing its job and is not news. The bar
# sits well above the hottest ground ever measured on Earth (80.8 C, Zhao et
# al. 2021) minus room for a hot desert day, so what clears it is unambiguously
# combustion rather than sunlight.
WILDFIRE_ANOMALY_MIN_C = 70.0

# Bins per row must exceed the widest possible longitude span (-180..180 is
# 18000 bins, 18002 once the guard ring is included) so that packing a
# (lat_bin, lon_bin) pair into one integer cannot alias one row onto the next.
FIRE_KEY_STRIDE = 20000

_GUARD_RING_OFFSETS = np.array(
    [d_lat * FIRE_KEY_STRIDE + d_lon for d_lat in (-1, 0, 1) for d_lon in (-1, 0, 1)],
    dtype=np.int64,
)


def fire_bin_keys(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Pack each coordinate's 0.02-degree bin into a single int64 key."""
    lat_bin = np.floor(np.asarray(lat, dtype=np.float64) / FIRE_BIN_DEGREES).astype(np.int64)
    lon_bin = np.floor(np.asarray(lon, dtype=np.float64) / FIRE_BIN_DEGREES).astype(np.int64)
    return lat_bin * FIRE_KEY_STRIDE + lon_bin


def fire_exclusion_keys(fire_lat: np.ndarray, fire_lon: np.ndarray) -> np.ndarray:
    """Bin keys covering every fire detection and its eight neighbouring bins.

    Returned sorted and unique. A granule with no detections yields an empty
    array, which every consumer reads as "checked, nothing burning" -- distinct
    from ``None``, which means the fire granule was never available.

    The ring does not wrap at the antimeridian or the poles: a fire within one
    bin of 180 degrees longitude guards only its own side. Losing a 2 km ring in
    the Pacific is not worth the complexity of wrapping arithmetic.
    """
    fire_lat = np.asarray(fire_lat, dtype=np.float64).ravel()
    fire_lon = np.asarray(fire_lon, dtype=np.float64).ravel()
    if fire_lat.shape != fire_lon.shape:
        raise UnexpectedGranuleError(
            f"fire latitude shape {fire_lat.shape} does not match longitude {fire_lon.shape}"
        )

    on_globe = geolocation_valid(fire_lat, fire_lon)
    if not on_globe.any():
        return np.empty(0, dtype=np.int64)

    centres = fire_bin_keys(fire_lat[on_globe], fire_lon[on_globe])
    return np.unique((centres[:, None] + _GUARD_RING_OFFSETS[None, :]).ravel())


def fire_excluded_mask(
    lat: np.ndarray, lon: np.ndarray, exclusion_keys: np.ndarray
) -> np.ndarray:
    """Pixels sitting in a fire bin or one of its neighbours."""
    keys = np.asarray(exclusion_keys, dtype=np.int64)
    if keys.size == 0:
        return np.zeros(np.shape(lat), dtype=bool)
    return np.isin(fire_bin_keys(lat, lon), keys)


# --- Aggregation --------------------------------------------------------------------


@dataclass(frozen=True)
class TileMax:
    """Hottest valid pixel seen so far inside one 1-degree tile."""

    tile_lat: int
    tile_lon: int
    max_c: float
    max_lat: float
    max_lon: float
    observed_at: str
    granule_id: str
    qc_note: str = QC_NOTE

    @property
    def key(self) -> tuple[int, int]:
        return (self.tile_lat, self.tile_lon)


def tile_maxima(
    celsius: np.ndarray,
    valid: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    observed_at: str,
    granule_id: str,
    qc_note: str = QC_NOTE,
    fire_tiles: Collection[tuple[int, int]] = (),
    outlier_tiles: Collection[tuple[int, int]] = (),
    volcanic_tiles: Collection[tuple[int, int]] = (),
) -> dict[tuple[int, int], TileMax]:
    """Hottest valid pixel per 1-degree tile within a single granule.

    ``fire_tiles``, ``outlier_tiles`` and ``volcanic_tiles`` name the tiles that
    lost at least one pixel to the active fire mask, to the latitude
    plausibility screen and to the volcanic screen. Each gets the corresponding
    note appended, so a row says outright that its maximum was taken after
    something was removed, and which something.
    """
    celsius = np.asarray(celsius, dtype=np.float64)
    burnt = frozenset(fire_tiles)
    implausible = frozenset(outlier_tiles)
    volcanic = frozenset(volcanic_tiles)
    keep = np.asarray(valid, dtype=bool) & geolocation_valid(lat, lon)
    if not keep.any():
        return {}

    flat_c = celsius[keep]
    flat_lat = np.asarray(lat, dtype=np.float64)[keep]
    flat_lon = np.asarray(lon, dtype=np.float64)[keep]
    t_lat, t_lon = tile_indices(flat_lat, flat_lon)

    # Group by a single integer key, then let a stable sort by (key, temperature)
    # put each tile's hottest pixel last within its run.
    keys = (t_lat - TILE_LAT_MIN) * 360 + (t_lon - TILE_LON_MIN)
    order = np.lexsort((flat_c, keys))
    sorted_keys = keys[order]
    is_last_of_run = np.empty(sorted_keys.shape, dtype=bool)
    is_last_of_run[-1] = True
    if sorted_keys.size > 1:
        is_last_of_run[:-1] = sorted_keys[:-1] != sorted_keys[1:]
    picks = order[is_last_of_run]

    result: dict[tuple[int, int], TileMax] = {}
    for i in picks:
        key = (int(t_lat[i]), int(t_lon[i]))
        note = qc_note
        if key in volcanic:
            note += VOLCANIC_MASKED_NOTE
        if key in burnt:
            note += FIRE_MASKED_NOTE
        if key in implausible:
            note += HIGH_LATITUDE_OUTLIER_NOTE
        entry = TileMax(
            tile_lat=key[0],
            tile_lon=key[1],
            max_c=float(flat_c[i]),
            max_lat=float(flat_lat[i]),
            max_lon=float(flat_lon[i]),
            observed_at=observed_at,
            granule_id=granule_id,
            qc_note=note,
        )
        result[entry.key] = entry
    return result


def merge_tile_maxima(
    accumulator: dict[tuple[int, int], TileMax],
    incoming: Mapping[tuple[int, int], TileMax],
) -> dict[tuple[int, int], TileMax]:
    """Fold one granule's tile maxima into the running day total, in place."""
    for key, candidate in incoming.items():
        current = accumulator.get(key)
        if current is None or candidate.max_c > current.max_c:
            accumulator[key] = candidate
    return accumulator


def reported_tile_count(
    ranked_max_c: Sequence[float],
    threshold_c: float = HOT_TILE_THRESHOLD_C,
    top_n: int = TOP_TILE_COUNT,
) -> int:
    """How many of a hottest-first list of temperatures the policy keeps.

    The selection rule itself, separated from the tiles it is applied to, so the
    daily table and the all-time table cannot drift apart on what counts as
    worth storing.
    """
    # The list is hottest first, so both the above-threshold set and the top N
    # are prefixes of it; their union is the longer prefix.
    hot_count = sum(1 for value in ranked_max_c if value >= threshold_c)
    return max(hot_count, min(top_n, len(ranked_max_c)))


def select_reported_tiles(
    tiles: Iterable[TileMax],
    threshold_c: float = HOT_TILE_THRESHOLD_C,
    top_n: int = TOP_TILE_COUNT,
) -> list[TileMax]:
    """Tiles worth storing: everything at or above the threshold, plus the global top N.

    The top-N clause is what keeps the map non-empty in a cool northern winter,
    when no tile anywhere clears 40 C. Returned hottest first.
    """
    ranked = sorted(tiles, key=lambda t: (-t.max_c, t.tile_lat, t.tile_lon))
    return ranked[: reported_tile_count([t.max_c for t in ranked], threshold_c, top_n)]


# --- Anomalies ----------------------------------------------------------------------

# Decision 2026-09-02. The weather archive holds corroborated weather and
# nothing else; heat that is real but not weather gets its own section rather
# than contaminating the archive or being silently dropped. Four things end up
# there, and each one says which it is:
#
# * volcanic -- inside a curated vent's radius. Real ground, not weather.
# * wildfire -- excluded by the active fire mask and hot enough to be notable.
#   These used to be discarded without trace.
# * failed corroboration -- record-tier, and the other satellite contradicted
#   it. Still published in lst_readings; barred from the archive.
# * uncorroborated -- record-tier, and no other satellite saw it at all. These
#   used to be kept in the archive with a caveat; as of this decision an
#   unverifiable record is not a record.


@dataclass(frozen=True)
class Anomaly:
    """One non-weather reading, with the worded reason it is not weather.

    Wraps the :class:`TileMax` rather than restating it, so an anomaly row and
    a weather row carry exactly the same measurement fields and cannot drift.
    """

    tile: TileMax
    cause: str
    source_slug: str | None = None

    @property
    def key(self) -> tuple[int, int, str]:
        """Matches the unique constraint on kiln.anomaly_readings, less the date
        and product the writer supplies."""
        return (self.tile.tile_lat, self.tile.tile_lon, self.cause)


def merge_anomalies(
    accumulator: dict[tuple[int, int, str], Anomaly],
    incoming: Mapping[tuple[int, int, str], Anomaly],
) -> dict[tuple[int, int, str], Anomaly]:
    """Fold one granule's anomalies into the running day total, in place.

    One row per tile per cause per day, holding the hottest reading of that
    cause -- the same maximum rule the weather tiles use, so a vent seen on
    three overpasses is one row and not three.
    """
    for key, candidate in incoming.items():
        current = accumulator.get(key)
        if current is None or candidate.tile.max_c > current.tile.max_c:
            accumulator[key] = candidate
    return accumulator


# --- Cross-satellite corroboration --------------------------------------------------

# Terra and Aqua cross the same ground about 90 minutes apart. Near local noon
# the ground does not change much over that interval, so two very different
# readings of the same tile on the same day are not two temperatures -- they are
# one temperature and one artifact.
#
# The case that motivated this: 2014-05-20, tile (12, 29) in Sudan. Terra read
# 85.73 C at 09:20 UTC and Aqua read 57.77 C at 10:45 UTC. Ground cannot shed
# 28 K in 85 minutes heading toward midday. The Terra value cleared both the QC
# bitmask and the MOD14 fire mask -- a sub-detection flare or a retrieval
# artifact -- and without this screen it would have become a permanent all-time
# record, because the archive merges by maximum and a maximum never comes back.
#
# The screen applies only to record-tier readings. Below the threshold a
# disagreement is ordinary diurnal and terrain variation and means nothing.
CORROBORATION_THRESHOLD_C = 78.0

# How far two satellites may differ on the same tile and still be believed. Wide
# enough for real diurnal drift and a genuine 90-minute swing on dark desert
# floor; far narrower than the 28 K that exposed Sudan.
CORROBORATION_TOLERANCE_K = 12.0

# The raster ceiling for a tile whose record-tier reading nothing corroborated.
# Sub-record heat in that tile is ordinary and stays; everything record-tier in
# it is as unverified as the maximum was. One hundredth below the threshold
# because the raster stores hundredths and drops what is strictly above the
# ceiling, so this takes out the reading sitting exactly on the threshold too.
UNCORROBORATED_CEILING_C = CORROBORATION_THRESHOLD_C - 0.01


@dataclass(frozen=True)
class Corroboration:
    """What the cross-satellite screen decided about a day's tiles."""

    # The day's corroborated weather: rejected winners replaced by the reading
    # that survived. This, and only this, is what may fossilize in the archive.
    tiles: dict[tuple[int, int], TileMax]

    # Readings barred from the archive. Real observations, still published in
    # the daily table, carrying the note that says why they stop there.
    rejected: dict[tuple[int, int], TileMax]

    # Record-tier readings no second satellite saw at all. As of the 2026-09-02
    # decision these leave the weather path too and are published as anomalies:
    # a record nothing can verify is not a record.
    uncorroborated: dict[tuple[int, int], TileMax]

    # Per tile the screen ruled against, the highest temperature the raster may
    # still show there. Pixels above it are dropped, since nothing corroborates
    # them either.
    ceilings: dict[tuple[int, int], float]

    @property
    def annotated(self) -> list[TileMax]:
        """Every reading whose note the screen changed, for the daily table."""
        return list(self.rejected.values()) + list(self.uncorroborated.values())

    @property
    def anomalies(self) -> list[Anomaly]:
        """What the screen sends to the anomalies section, worded cause and all."""
        return [
            Anomaly(tile=tile, cause=CAUSE_FAILED_CORROBORATION)
            for tile in self.rejected.values()
        ] + [
            Anomaly(tile=tile, cause=CAUSE_UNCORROBORATED)
            for tile in self.uncorroborated.values()
        ]


def _annotate(tile: TileMax, note: str) -> TileMax:
    return replace(tile, qc_note=tile.qc_note + note)


def corroborate_day(
    per_product: Mapping[str, Mapping[tuple[int, int], TileMax]],
    threshold_c: float = CORROBORATION_THRESHOLD_C,
    tolerance_k: float = CORROBORATION_TOLERANCE_K,
) -> Corroboration:
    """Screen a day's per-tile maxima against the other satellite's view.

    For each tile, the hottest reading of the day is the candidate:

    * Below ``threshold_c`` it passes untouched. Ordinary readings are not worth
      second-guessing, and a screen that fired everywhere would be a screen
      nobody could reason about.
    * With a second satellite's reading within ``tolerance_k``, it passes
      untouched: two instruments agree.
    * With a second satellite's reading further away than that, it is rejected
      and the cooler reading stands in its place. If that survivor is itself
      record-tier, it too has lost its only witness and follows the same road.
    * With no second reading at all -- cloud over the other overpass, an orbit
      gap, a single-product or pre-Aqua run -- it leaves the weather path and is
      published as an anomaly instead (decision 2026-09-02). It used to be kept
      in the archive with a caveat. The caveat was true and it was not enough:
      the archive's claim is that its numbers are corroborated measurements, and
      a maximum nothing can check does not meet it. The reading is not lost --
      it stays in the daily table and gains an anomaly row that names why.

    ``per_product`` maps each product to its own tile maxima, so a single
    ``--product`` run has nothing to corroborate against and every record-tier
    tile in it becomes an anomaly, which is exactly true.
    """
    keys: set[tuple[int, int]] = set()
    for tiles in per_product.values():
        keys.update(tiles)

    screened: dict[tuple[int, int], TileMax] = {}
    rejected: dict[tuple[int, int], TileMax] = {}
    uncorroborated: dict[tuple[int, int], TileMax] = {}
    ceilings: dict[tuple[int, int], float] = {}

    for key in sorted(keys):
        readings = sorted(
            (tiles[key] for tiles in per_product.values() if key in tiles),
            key=lambda tile: -tile.max_c,
        )
        winner, others = readings[0], readings[1:]

        if winner.max_c < threshold_c:
            screened[key] = winner
            continue

        if not others:
            uncorroborated[key] = _annotate(winner, UNCORROBORATED_NOTE)
            ceilings[key] = UNCORROBORATED_CEILING_C
            continue

        witness = others[0]
        if winner.max_c - witness.max_c <= tolerance_k:
            screened[key] = winner
            continue

        rejected[key] = _annotate(winner, CORROBORATION_REJECTED_NOTE)
        if witness.max_c >= threshold_c:
            uncorroborated[key] = _annotate(witness, UNCORROBORATED_NOTE)
            ceilings[key] = UNCORROBORATED_CEILING_C
        else:
            screened[key] = witness
            ceilings[key] = witness.max_c

    return Corroboration(
        tiles=screened,
        rejected=rejected,
        uncorroborated=uncorroborated,
        ceilings=ceilings,
    )


# --- Whole-granule composition ------------------------------------------------------


@dataclass(frozen=True)
class GranuleField:
    """One granule decoded to Celsius, with everything unusable already masked.

    Both consumers of a granule read this: the 1-degree tile maxima that reach
    ``lst_readings`` and the web-mercator raster pyramid. Sharing the field is
    what guarantees a pixel dropped for fire, cloud, implausibility or bad QC
    cannot reach one output while being excluded from the other.
    """

    celsius: np.ndarray
    keep: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    fire_tiles: frozenset[tuple[int, int]] = frozenset()
    outlier_tiles: frozenset[tuple[int, int]] = frozenset()
    volcanic_tiles: frozenset[tuple[int, int]] = frozenset()

    # The pixels the fire and volcanic screens took out, kept so the anomalies
    # section can report the hottest of them. ``None`` where the screen removed
    # nothing, which is the common case and costs no array.
    fire_pixels: np.ndarray | None = None
    volcanic_pixels: np.ndarray | None = None


def prepare_granule(
    raw_lst: np.ndarray,
    lst_attrs: Mapping[str, object],
    qc: np.ndarray,
    lat_sub: np.ndarray,
    lon_sub: np.ndarray,
    max_error_class: int = 1,
    fire_exclusion: np.ndarray | None = None,
    volcanic_sources: Sequence[VolcanicSource] | None = None,
) -> GranuleField:
    """Full science path for one granule, from raw SDS arrays to a pixel field.

    Kept free of network I/O so tests can drive it with fabricated arrays;
    :mod:`kiln_ingest.granule` supplies the real ones.

    ``fire_exclusion`` is the bin-key array from :func:`fire_exclusion_keys`, or
    ``None`` when no fire granule was available for this overpass.
    ``volcanic_sources`` defaults to the bundled curated list; pass an empty
    sequence to run without the volcanic screen. The latitude plausibility
    screen always runs; it needs no external data and is policy, not an option.
    """
    raw = np.asarray(raw_lst)
    qc_arr = np.asarray(qc)
    if raw.shape != qc_arr.shape:
        raise UnexpectedGranuleError(
            f"LST shape {raw.shape} does not match QC shape {qc_arr.shape}"
        )
    if raw.ndim != 2:
        raise UnexpectedGranuleError("LST SDS must be 2-dimensional")

    scaling = resolve_lst_scaling(lst_attrs)
    celsius, valid = decode_lst_celsius(raw, scaling)
    valid &= qc_keep_mask(qc_arr, max_error_class=max_error_class)

    lat, lon = expand_geolocation(lat_sub, lon_sub, raw.shape)
    keep = valid & geolocation_valid(lat, lon)

    # Volcanic first, before the fire mask, because a lava lake trips MOD14 as
    # readily as a wildfire does and the two are not the same fact. Classifying
    # by the curated list first means Erta Ale is published as a volcano rather
    # than filed as an unnamed fire, and the fire mask then sees only what the
    # vent list did not account for.
    volcanic_tiles: frozenset[tuple[int, int]] = frozenset()
    volcanic_pixels: np.ndarray | None = None
    sources = resolve_volcanic_sources(volcanic_sources)
    if sources:
        erupting = keep & volcanic_excluded_mask(lat, lon, sources)
        if erupting.any():
            volcanic_tiles = tiles_covering(lat, lon, erupting)
            volcanic_pixels = erupting
            keep = keep & ~erupting

    # Fire next, then plausibility: the fire mask names a specific cause, so a
    # pixel it accounts for should be reported as burnt rather than as a
    # nameless outlier. What reaches the screen is what MOD14 did not detect.
    fire_tiles: frozenset[tuple[int, int]] = frozenset()
    fire_pixels: np.ndarray | None = None
    if fire_exclusion is not None and np.size(fire_exclusion) > 0:
        burning = keep & fire_excluded_mask(lat, lon, fire_exclusion)
        if burning.any():
            fire_tiles = tiles_covering(lat, lon, burning)
            fire_pixels = burning
            keep = keep & ~burning

    outlier_tiles: frozenset[tuple[int, int]] = frozenset()
    implausible = keep & ~plausibility_keep_mask(celsius, lat)
    if implausible.any():
        outlier_tiles = tiles_covering(lat, lon, implausible)
        keep = keep & ~implausible

    return GranuleField(
        celsius=celsius,
        keep=keep,
        lat=lat,
        lon=lon,
        fire_tiles=fire_tiles,
        outlier_tiles=outlier_tiles,
        volcanic_tiles=volcanic_tiles,
        fire_pixels=fire_pixels,
        volcanic_pixels=volcanic_pixels,
    )


def granule_tile_maxima(
    raw_lst: np.ndarray,
    lst_attrs: Mapping[str, object],
    qc: np.ndarray,
    lat_sub: np.ndarray,
    lon_sub: np.ndarray,
    observed_at: str,
    granule_id: str,
    max_error_class: int = 1,
    fire_exclusion: np.ndarray | None = None,
    qc_note: str = QC_NOTE,
    volcanic_sources: Sequence[VolcanicSource] | None = None,
) -> dict[tuple[int, int], TileMax]:
    """Tile maxima for one granule, discarding the pixel field."""
    field = prepare_granule(
        raw_lst=raw_lst,
        lst_attrs=lst_attrs,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        max_error_class=max_error_class,
        fire_exclusion=fire_exclusion,
        volcanic_sources=volcanic_sources,
    )
    return tile_maxima(
        field.celsius,
        field.keep,
        field.lat,
        field.lon,
        observed_at=observed_at,
        granule_id=granule_id,
        qc_note=qc_note,
        fire_tiles=field.fire_tiles,
        outlier_tiles=field.outlier_tiles,
        volcanic_tiles=field.volcanic_tiles,
    )


def granule_anomalies(
    field: GranuleField,
    observed_at: str,
    granule_id: str,
    volcanic_sources: Sequence[VolcanicSource] | None = None,
    volcanic_min_c: float = VOLCANIC_ANOMALY_MIN_C,
    wildfire_min_c: float = WILDFIRE_ANOMALY_MIN_C,
) -> dict[tuple[int, int, str], Anomaly]:
    """The non-weather readings one granule contributes, hottest per tile and cause.

    Reads the pixels the two exclusion screens set aside. Everything here was
    already removed from ``field.keep``, so nothing published as an anomaly can
    also be published as weather -- the two sets are disjoint by construction
    rather than by agreement between two pieces of code.

    Each cause has its own floor. Every pixel near a vent is technically
    volcanic, and most of them are just warm ground; only a reading hot enough
    to be worth showing earns a row.
    """
    sources = resolve_volcanic_sources(volcanic_sources)
    found: dict[tuple[int, int, str], Anomaly] = {}

    if field.volcanic_pixels is not None:
        hottest = tile_maxima(
            field.celsius,
            field.volcanic_pixels,
            field.lat,
            field.lon,
            observed_at=observed_at,
            granule_id=granule_id,
            qc_note=QC_NOTE + VOLCANIC_ANOMALY_NOTE,
        )
        for tile in hottest.values():
            if tile.max_c < volcanic_min_c:
                continue
            vent = nearest_vent(tile.max_lat, tile.max_lon, sources)
            anomaly = Anomaly(
                tile=tile,
                cause=CAUSE_VOLCANIC,
                source_slug=None if vent is None else vent.slug,
            )
            found[anomaly.key] = anomaly

    if field.fire_pixels is not None:
        hottest = tile_maxima(
            field.celsius,
            field.fire_pixels,
            field.lat,
            field.lon,
            observed_at=observed_at,
            granule_id=granule_id,
            qc_note=QC_NOTE + ACTIVE_FIRE_ANOMALY_NOTE,
        )
        for tile in hottest.values():
            if tile.max_c < wildfire_min_c:
                continue
            # No source_slug: a wildfire is not a place, and naming one would
            # imply a citation this row does not have.
            anomaly = Anomaly(tile=tile, cause=CAUSE_WILDFIRE)
            found[anomaly.key] = anomaly

    return found
