"""Web-mercator raster accumulation for the Kiln live layer.

The 1-degree tiles in ``lst_readings`` are a summary: one number per ~111 km
cell. This module keeps the measurement at its native resolution instead,
painting every hot pixel into a global XYZ tile pyramid so the map can be
zoomed into a single 1 km MODIS footprint.

Everything here is plain numpy on plain dicts: no file, no network, no image
library. :mod:`kiln_ingest.tile_png` turns the arrays into PNGs and
:mod:`kiln_ingest.storage_io` publishes them.

Zoom 7 is the base level -- 256 px tiles across 2^7 tiles is 32768 px around
the equator, about 1.2 km per pixel, which is as close to the 1 km MODIS grid
as a power-of-two pyramid gets without inventing detail. Coarser levels are
max-pooled from it, so a hotspot survives all the way out to the world view
instead of being averaged into its surroundings.
"""

from __future__ import annotations

from typing import Mapping, MutableMapping

import numpy as np

from .science import HOT_TILE_THRESHOLD_C, geolocation_valid, tile_indices

TILE_SIZE = 256
MIN_ZOOM = 0
MAX_ZOOM = 7

# Web mercator is undefined at the poles and conventionally truncated here,
# which makes the projected world exactly square.
MERCATOR_MAX_LAT = 85.05112878

# Tiles hold hundredths of a degree Celsius as int16: 200 C, the top of the
# science core's plausibility band, is 20000, well inside the type. -32768 is
# reserved as "no observation", which is what makes cloud gaps stay gaps.
EMPTY_CENTI_C = -32768
CENTI_PER_DEGREE = 100

# Only pixels worth displaying are rasterised, which is what keeps the pyramid
# to a few thousand tiles instead of the whole globe.
RASTER_MIN_C = HOT_TILE_THRESHOLD_C

# Each base tile is 256 * 256 * 2 bytes = 128 KB held in memory until upload.
# A hot day covers a few percent of the globe, so a few thousand tiles is
# normal and roughly 0.5 GB; past this the runner is heading for trouble and
# the count is worth shouting about.
ACTIVE_TILE_WARN = 8000
TILE_BYTES = TILE_SIZE * TILE_SIZE * 2

# (tile x, tile y) -> 256x256 int16 array, at one zoom level.
TileStore = MutableMapping[tuple[int, int], np.ndarray]


def global_pixel_extent(zoom: int) -> int:
    """Width and height of the whole world in pixels at ``zoom``."""
    return TILE_SIZE * (1 << zoom)


def project_to_pixels(
    lat: np.ndarray, lon: np.ndarray, zoom: int = MAX_ZOOM
) -> tuple[np.ndarray, np.ndarray]:
    """Global XYZ pixel coordinates for each coordinate pair.

    Latitude is clamped to the web-mercator limit rather than dropped: a
    genuinely hot pixel above 85 degrees would be a remarkable measurement, and
    pinning it to the edge of the map is more honest than silently losing it.
    """
    extent = global_pixel_extent(zoom)
    lat = np.clip(np.asarray(lat, dtype=np.float64), -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT)
    lon = np.asarray(lon, dtype=np.float64)

    x = (lon + 180.0) / 360.0 * extent
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * extent

    px = np.clip(np.floor(x), 0, extent - 1).astype(np.int64)
    py = np.clip(np.floor(y), 0, extent - 1).astype(np.int64)
    return px, py


def to_centi_celsius(celsius: np.ndarray) -> np.ndarray:
    """Celsius to the int16 hundredths stored in a tile."""
    centi = np.rint(np.asarray(celsius, dtype=np.float64) * CENTI_PER_DEGREE)
    return np.clip(centi, EMPTY_CENTI_C + 1, np.iinfo(np.int16).max).astype(np.int16)


def blank_tile() -> np.ndarray:
    return np.full((TILE_SIZE, TILE_SIZE), EMPTY_CENTI_C, dtype=np.int16)


def accumulate_granule(
    store: TileStore,
    celsius: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    valid: np.ndarray | None = None,
    zoom: int = MAX_ZOOM,
    threshold_c: float = RASTER_MIN_C,
) -> TileStore:
    """Paint one granule's hot pixels into the base-zoom store, in place.

    Every write is a maximum, so folding granules in any order -- and both
    satellites into the same store -- gives the day's hottest observation per
    pixel. Overlapping swaths and the second satellite's pass do not overwrite
    a hotter earlier reading.
    """
    celsius = np.asarray(celsius, dtype=np.float64).ravel()
    lat = np.asarray(lat, dtype=np.float64).ravel()
    lon = np.asarray(lon, dtype=np.float64).ravel()

    keep = celsius >= threshold_c
    if valid is not None:
        keep &= np.asarray(valid, dtype=bool).ravel()
    keep &= geolocation_valid(lat, lon)
    if not keep.any():
        return store

    px, py = project_to_pixels(lat[keep], lon[keep], zoom=zoom)
    centi = to_centi_celsius(celsius[keep])

    # Several 1 km MODIS pixels can land on one tile pixel, especially away
    # from nadir. Collapse them to the hottest first so the scatter below
    # writes each destination exactly once: duplicate fancy-index writes have
    # no defined winner.
    extent = global_pixel_extent(zoom)
    flat = py * extent + px
    order = np.lexsort((centi, flat))
    sorted_flat = flat[order]
    is_last_of_run = np.empty(sorted_flat.shape, dtype=bool)
    is_last_of_run[-1] = True
    if sorted_flat.size > 1:
        is_last_of_run[:-1] = sorted_flat[:-1] != sorted_flat[1:]
    picks = order[is_last_of_run]

    px, py, centi = px[picks], py[picks], centi[picks]
    tile_x, local_x = np.divmod(px, TILE_SIZE)
    tile_y, local_y = np.divmod(py, TILE_SIZE)

    # A swath crosses only a few dozen base tiles, so grouping by tile and
    # writing each in one vectorised shot is cheap.
    tile_key = tile_y * (1 << zoom) + tile_x
    _, inverse = np.unique(tile_key, return_inverse=True)
    for group in range(int(inverse.max()) + 1):
        selected = inverse == group
        key = (int(tile_x[selected][0]), int(tile_y[selected][0]))
        tile = store.get(key)
        if tile is None:
            tile = blank_tile()
            store[key] = tile
        rows = local_y[selected]
        cols = local_x[selected]
        tile[rows, cols] = np.maximum(tile[rows, cols], centi[selected])

    return store


def pixel_center_degrees(
    tile_x: int, tile_y: int, zoom: int = MAX_ZOOM
) -> tuple[np.ndarray, np.ndarray]:
    """Latitude of each pixel row and longitude of each pixel column of a tile.

    The inverse of :func:`project_to_pixels`, taken at pixel centres. Mercator
    is cylindrical, so latitude varies only down the rows and longitude only
    across the columns -- which is why two 1-D axes suffice, and why a 1-degree
    tile covers an axis-aligned block of pixels rather than a ragged region.
    """
    extent = global_pixel_extent(zoom)
    columns = tile_x * TILE_SIZE + np.arange(TILE_SIZE) + 0.5
    rows = tile_y * TILE_SIZE + np.arange(TILE_SIZE) + 0.5

    lon = columns / extent * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * rows / extent))))
    return lat, lon


def drop_above_ceilings(
    store: TileStore,
    ceilings: Mapping[tuple[int, int], float],
    zoom: int = MAX_ZOOM,
) -> int:
    """Remove pixels a cross-satellite-rejected 1-degree tile cannot support.

    ``ceilings`` maps a 1-degree tile to the surviving satellite's maximum.
    Pixels above it are dropped rather than clamped down to it: lowering a pixel
    to the ceiling would publish a temperature no instrument recorded, which is
    a worse answer than an honest gap.

    Pixels are assigned to 1-degree tiles with the same :func:`tile_indices`
    the maxima used, so the raster and the table cannot disagree about which
    tile a pixel belongs to. Returns the number of pixels dropped.
    """
    if not ceilings:
        return 0

    limits = {
        key: int(to_centi_celsius(np.asarray([value]))[0]) for key, value in ceilings.items()
    }
    dropped = 0

    for (tile_x, tile_y), tile in store.items():
        lat, lon = pixel_center_degrees(tile_x, tile_y, zoom)
        rows, columns = tile_indices(lat, lon)
        row_tiles = set(rows.tolist())
        column_tiles = set(columns.tolist())

        for (tile_lat, tile_lon), limit in limits.items():
            if tile_lat not in row_tiles or tile_lon not in column_tiles:
                continue
            inside = (rows[:, None] == tile_lat) & (columns[None, :] == tile_lon)
            doomed = inside & (tile > limit)
            count = int(doomed.sum())
            if count:
                tile[doomed] = EMPTY_CENTI_C
                dropped += count

    return dropped


def downsample_children(
    children: Mapping[tuple[int, int], np.ndarray], parent_x: int, parent_y: int
) -> np.ndarray | None:
    """One parent tile from up to four children, 2x2 max-pooled into quadrants.

    Returns ``None`` when no child exists, so empty regions never become tiles.
    """
    parent: np.ndarray | None = None
    half = TILE_SIZE // 2

    for offset_x in (0, 1):
        for offset_y in (0, 1):
            child = children.get((parent_x * 2 + offset_x, parent_y * 2 + offset_y))
            if child is None:
                continue
            if parent is None:
                parent = blank_tile()
            # Max of each 2x2 block. EMPTY_CENTI_C is the minimum of the type,
            # so a block only stays empty when all four of its pixels are.
            pooled = child.reshape(half, 2, half, 2).max(axis=(1, 3))
            parent[
                offset_y * half : (offset_y + 1) * half,
                offset_x * half : (offset_x + 1) * half,
            ] = pooled

    return parent


def build_pyramid(
    base: Mapping[tuple[int, int], np.ndarray],
    max_zoom: int = MAX_ZOOM,
    min_zoom: int = MIN_ZOOM,
) -> dict[int, dict[tuple[int, int], np.ndarray]]:
    """Zoom -> tile store, from the base level down to ``min_zoom``."""
    if min_zoom > max_zoom:
        raise ValueError(f"min_zoom {min_zoom} is above max_zoom {max_zoom}")

    pyramid: dict[int, dict[tuple[int, int], np.ndarray]] = {max_zoom: dict(base)}
    for zoom in range(max_zoom - 1, min_zoom - 1, -1):
        children = pyramid[zoom + 1]
        parent_keys = {(x // 2, y // 2) for x, y in children}
        level: dict[tuple[int, int], np.ndarray] = {}
        for parent_x, parent_y in parent_keys:
            tile = downsample_children(children, parent_x, parent_y)
            if tile is not None:
                level[(parent_x, parent_y)] = tile
        pyramid[zoom] = level
    return pyramid


def tile_has_data(tile: np.ndarray) -> bool:
    return bool((np.asarray(tile) != EMPTY_CENTI_C).any())


def store_memory_mb(store: Mapping[tuple[int, int], np.ndarray]) -> float:
    return len(store) * TILE_BYTES / (1024 * 1024)
