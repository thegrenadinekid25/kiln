"""Paletted PNG encoding for the Kiln raster tiles.

The ramp is the site's ramp. The thresholds and hex values below mirror
``--heat-1`` .. ``--heat-5`` in ``web/src/tokens.css`` and the MapLibre ``step``
expression in ``web/src/components/LiveLayer/LiveLayer.tsx``; a raster tile and
a 1-degree cell showing the same temperature must be the same colour, so the
three places change together or not at all.

Six palette entries, index 0 transparent. Pixels the pipeline never observed
stay transparent rather than being painted a "cold" colour: a cloud gap is
absence of measurement, not a cool reading.

Pillow is imported lazily inside the encoder so the science core, the raster
maths and their tests stay importable on a machine without it.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np

from .raster import CENTI_PER_DEGREE, EMPTY_CENTI_C

# Upper edge of each band, in Celsius. Below the first is heat-1.
HEAT_STEPS_C: tuple[float, ...] = (50.0, 58.0, 66.0, 74.0)

# --heat-1 .. --heat-5, in order. The raster carries nothing below 40 C, so
# heat-1 covers 40-50 C here.
HEAT_COLORS: tuple[str, ...] = ("#C9B896", "#C79B5B", "#BC7431", "#9A4E17", "#6E3410")

TRANSPARENT_INDEX = 0
PALETTE_ENTRIES = 256


def _import_pillow() -> Any:
    try:
        from PIL import Image  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "Pillow is required to encode raster tiles. Install it with "
            "'pip install -r requirements.txt'."
        ) from exc
    return Image


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def palette_bytes() -> bytes:
    """The flat RGB palette: index 0 unused-and-transparent, 1-5 the heat ramp."""
    table = bytearray(PALETTE_ENTRIES * 3)
    for index, color in enumerate(HEAT_COLORS, start=1):
        red, green, blue = _rgb(color)
        table[index * 3 : index * 3 + 3] = bytes((red, green, blue))
    return bytes(table)


def palette_indices(tile: np.ndarray) -> np.ndarray:
    """Map a centi-Celsius tile onto palette indices, 0 for unobserved pixels."""
    values = np.asarray(tile)
    cuts = np.array(
        [round(step * CENTI_PER_DEGREE) for step in HEAT_STEPS_C], dtype=np.int32
    )
    indices = (np.digitize(values.astype(np.int32), cuts, right=False) + 1).astype(np.uint8)
    indices[values == EMPTY_CENTI_C] = TRANSPARENT_INDEX
    return indices


def encode_indices_png(indices: np.ndarray) -> bytes:
    """A palette-index array as a paletted PNG with a transparent background.

    The stored index values survive a round trip through
    :func:`decode_indices_png` unchanged, including the sparse index sets
    ``optimize=True`` might otherwise be tempted to renumber. The all-time
    pyramid depends on that: it merges parent tiles by comparing ranks.
    """
    Image = _import_pillow()
    indices = np.ascontiguousarray(indices, dtype=np.uint8)
    height, width = indices.shape

    image = Image.frombytes("P", (width, height), indices.tobytes())
    image.putpalette(palette_bytes())

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", transparency=TRANSPARENT_INDEX, optimize=True)
    return buffer.getvalue()


def decode_indices_png(png: bytes) -> np.ndarray:
    """The palette-index array of a tile this module wrote."""
    Image = _import_pillow()
    with Image.open(io.BytesIO(png)) as image:
        if image.mode != "P":
            raise ValueError(f"expected a paletted tile, got mode {image.mode}")
        return np.array(image, dtype=np.uint8)


def encode_tile_png(tile: np.ndarray) -> bytes:
    """One centi-Celsius tile as a paletted PNG with a transparent background."""
    return encode_indices_png(palette_indices(tile))
