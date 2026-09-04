"""All-time accumulation: the hottest reading Kiln has ever recorded per place.

The daily pyramid answers "how hot was it yesterday". This answers "how hot has
this ground ever got, in the years we have been watching" -- a running
elementwise maximum over every day the pipeline has processed, kept in the
``kiln-tiles`` bucket as base-zoom state arrays.

**The ordering that matters.** Everything merged here comes out of the day's
base-zoom raster store, which is painted only from pixels that already survived
the active-fire mask and the latitude plausibility screen. That order is not an
implementation detail: a merge is a maximum and a maximum is permanent, so a
single fire pixel admitted once becomes an all-time record that no later day can
undo. The screens run first, always, and nothing writes to this module's inputs
except :func:`kiln_ingest.raster.accumulate_granule` fed by a masked field.

**Why parents merge by palette rank.** Base-zoom state is exact centi-Celsius.
Coarser zooms are rebuilt from the tiles that changed today, merged into the
tiles already published -- and those are PNGs, which store palette ranks rather
than temperatures. That loses nothing, because the palette is a monotonic
non-decreasing function of temperature: the bucket of a maximum is the maximum
of the buckets. Merging ranks therefore gives exactly the image exact
max-pooling would have produced. The alternative, downloading every sibling's
state to rebuild a parent exactly, would mean fetching the whole globe to
rebuild zoom 0.

Everything here is pure: numpy, dicts, and two callbacks for the bytes it cannot
fetch itself.
"""

from __future__ import annotations

import io
import logging
from datetime import date
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

from .raster import MAX_ZOOM, MIN_ZOOM, TILE_SIZE
from .science import (
    HOT_TILE_THRESHOLD_C,
    TOP_TILE_COUNT,
    TileMax,
    reported_tile_count,
)
from .tile_png import TRANSPARENT_INDEX, palette_indices

LOG = logging.getLogger(__name__)

TileKey = tuple[int, int]
StateStore = Mapping[TileKey, np.ndarray]

# Fetches the published rank arrays for one zoom level, None where a tile does
# not exist yet.
FetchLevel = Callable[[int, Sequence[TileKey]], Mapping[TileKey, "np.ndarray | None"]]


class CorruptStateError(ValueError):
    """A stored all-time state object is not the array this pipeline wrote."""


# --- State serialisation ------------------------------------------------------------


def dump_state(state: np.ndarray) -> bytes:
    """One base-zoom state array as ``.npy`` bytes."""
    buffer = io.BytesIO()
    np.save(buffer, np.ascontiguousarray(state, dtype=np.int16), allow_pickle=False)
    return buffer.getvalue()


def load_state(data: bytes) -> np.ndarray:
    """Read back a state array, refusing anything that is not one.

    ``allow_pickle=False`` is not optional. A ``.npy`` file can carry a pickle,
    and pickles execute code on load; the bucket is world-readable and this
    process holds the service key, so the loader must never be the thing that
    trusts its input.
    """
    try:
        state = np.load(io.BytesIO(data), allow_pickle=False)
    except Exception as exc:  # noqa: BLE001 - any unreadable payload is corrupt
        raise CorruptStateError(f"state object could not be read: {exc}") from exc

    if state.shape != (TILE_SIZE, TILE_SIZE):
        raise CorruptStateError(f"state array has shape {state.shape}, expected square tile")
    if state.dtype != np.int16:
        raise CorruptStateError(f"state array has dtype {state.dtype}, expected int16")
    return state


# --- Merging the day into the archive -----------------------------------------------


def merge_state(today: np.ndarray, stored: np.ndarray | None) -> np.ndarray:
    """The all-time state for one tile after folding today into it.

    An absent stored array means today *is* the state. ``EMPTY_CENTI_C`` is the
    minimum of the type, so unobserved pixels lose to every real reading without
    needing to be special-cased.
    """
    today = np.asarray(today, dtype=np.int16)
    if stored is None:
        return today.copy()
    return np.maximum(today, np.asarray(stored, dtype=np.int16))


def merge_day(
    today: StateStore, stored: Mapping[TileKey, "np.ndarray | None"]
) -> dict[TileKey, np.ndarray]:
    """Every tile whose all-time state today actually changed.

    Tiles that today could not improve on are left out entirely: they need no
    upload and no pyramid rebuild, which is what keeps a daily run proportional
    to the day's new heat rather than to the size of the archive.
    """
    changed: dict[TileKey, np.ndarray] = {}
    for key in sorted(today):
        previous = stored.get(key)
        merged = merge_state(today[key], previous)
        if previous is None or not np.array_equal(merged, previous):
            changed[key] = merged
    return changed


# --- Rebuilding the pyramid over what changed ---------------------------------------


def pool_ranks(child: np.ndarray) -> np.ndarray:
    """2x2 max-pool a tile of palette ranks down to one quadrant of its parent."""
    half = TILE_SIZE // 2
    return np.asarray(child, dtype=np.uint8).reshape(half, 2, half, 2).max(axis=(1, 3))


def parent_key(key: TileKey) -> TileKey:
    x, y = key
    return (x // 2, y // 2)


def parent_keys(keys: Iterable[TileKey]) -> list[TileKey]:
    return sorted({parent_key(key) for key in keys})


def merge_parent_ranks(
    children: Mapping[TileKey, np.ndarray],
    parent: TileKey,
    existing: np.ndarray | None,
) -> np.ndarray:
    """One parent tile: its published ranks, raised wherever a child changed.

    Quadrants whose child did not change today are left exactly as published --
    the maximum of a quadrant against nothing is that quadrant.
    """
    merged = (
        np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
        if existing is None
        else np.array(existing, dtype=np.uint8, copy=True)
    )
    half = TILE_SIZE // 2
    parent_x, parent_y = parent

    for offset_x in (0, 1):
        for offset_y in (0, 1):
            child = children.get((parent_x * 2 + offset_x, parent_y * 2 + offset_y))
            if child is None:
                continue
            quadrant = merged[
                offset_y * half : (offset_y + 1) * half,
                offset_x * half : (offset_x + 1) * half,
            ]
            np.maximum(quadrant, pool_ranks(child), out=quadrant)

    return merged


def build_alltime_levels(
    changed: StateStore,
    fetch_level: FetchLevel,
    max_zoom: int = MAX_ZOOM,
    min_zoom: int = MIN_ZOOM,
) -> tuple[dict[int, dict[TileKey, np.ndarray]], set[tuple[int, TileKey]]]:
    """Rank arrays for every tile that has to be rewritten today.

    Returns ``(levels, created)`` where ``created`` names the tiles that did not
    exist before this run, which is what keeps the manifest's running total
    honest without listing the whole bucket.
    """
    if min_zoom > max_zoom:
        raise ValueError(f"min_zoom {min_zoom} is above max_zoom {max_zoom}")

    levels: dict[int, dict[TileKey, np.ndarray]] = {
        max_zoom: {key: palette_indices(state) for key, state in changed.items()}
    }
    created: set[tuple[int, TileKey]] = set()

    for zoom in range(max_zoom - 1, min_zoom - 1, -1):
        children = levels[zoom + 1]
        keys = parent_keys(children)
        published = fetch_level(zoom, keys)

        level: dict[TileKey, np.ndarray] = {}
        for key in keys:
            existing = published.get(key)
            if existing is None:
                created.add((zoom, key))
            level[key] = merge_parent_ranks(children, key, existing)
        levels[zoom] = level

    return levels, created


def level_has_data(ranks: np.ndarray) -> bool:
    return bool((np.asarray(ranks) != TRANSPARENT_INDEX).any())


# --- The all-time table -------------------------------------------------------------


def alltime_cutoff_c(
    all_max_c: Sequence[float],
    threshold_c: float = HOT_TILE_THRESHOLD_C,
    top_n: int = TOP_TILE_COUNT,
) -> float:
    """The coolest temperature still worth a row, under the daily selection policy.

    Applied to the whole archive rather than to one day, so a tile earns its row
    by its all-time maximum and not by how hot the rest of today happened to be.
    """
    ranked = sorted(all_max_c, reverse=True)
    if not ranked:
        return threshold_c
    return ranked[reported_tile_count(ranked, threshold_c, top_n) - 1]


def select_alltime_upserts(
    existing: Mapping[TileKey, float],
    today: Mapping[TileKey, TileMax],
    threshold_c: float = HOT_TILE_THRESHOLD_C,
    top_n: int = TOP_TILE_COUNT,
) -> list[TileMax]:
    """Today's readings that beat their tile's all-time record and earn a row.

    Two conditions, both required: the reading has to improve on what the
    archive already holds for that tile, and the improved value has to clear the
    same bar the daily table uses. Returned hottest first.
    """
    improved = {
        key: tile
        for key, tile in today.items()
        if key not in existing or tile.max_c > existing[key]
    }
    if not improved:
        return []

    merged = dict(existing)
    for key, tile in today.items():
        merged[key] = max(merged.get(key, tile.max_c), tile.max_c)

    cutoff = alltime_cutoff_c(list(merged.values()), threshold_c, top_n)
    return sorted(
        (tile for tile in improved.values() if tile.max_c >= cutoff),
        key=lambda t: (-t.max_c, t.tile_lat, t.tile_lon),
    )


# --- Manifest bookkeeping -----------------------------------------------------------


def alltime_since(prior_manifest: Mapping[str, object] | None, target: date) -> str:
    """The earliest date ever merged into the archive.

    Carried forward from the published manifest, because nothing else remembers
    it. A manifest that is missing or unreadable makes today the start of the
    record, which is right for a first run and honest for a lost one.
    """
    prior = (prior_manifest or {}).get("since")
    try:
        earliest = date.fromisoformat(str(prior))
    except (TypeError, ValueError):
        return target.isoformat()
    return min(earliest, target).isoformat()


def alltime_tile_total(
    prior_manifest: Mapping[str, object] | None, created: int
) -> int:
    """The archive's running tile count, grown by the tiles this run created.

    Counted rather than listed: enumerating a permanent bucket every day would
    cost more than the run that fills it.
    """
    try:
        previous = int((prior_manifest or {}).get("tile_count"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        previous = 0
    return max(previous, 0) + max(created, 0)
