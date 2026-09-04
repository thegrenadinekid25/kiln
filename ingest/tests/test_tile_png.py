"""PNG encoding tests: the ramp a tile is painted with is a contract with web/.

The expected colours here are written out literally rather than imported from
the encoder, so that changing --heat-1..5 in tokens.css without changing them
here fails loudly instead of silently re-colouring the map.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

from kiln_ingest.raster import EMPTY_CENTI_C, blank_tile
from kiln_ingest.tile_png import (
    decode_indices_png,
    encode_indices_png,
    HEAT_COLORS,
    PALETTE_ENTRIES,
    TRANSPARENT_INDEX,
    encode_tile_png,
    palette_bytes,
    palette_indices,
)

Image = pytest.importorskip("PIL.Image", reason="Pillow encodes the raster tiles")

# tokens.css --heat-1 .. --heat-5, and the LiveLayer step expression's breaks.
EXPECTED_RAMP = (
    (40.0, (0xC9, 0xB8, 0x96)),
    (49.99, (0xC9, 0xB8, 0x96)),
    (50.0, (0xC7, 0x9B, 0x5B)),
    (57.99, (0xC7, 0x9B, 0x5B)),
    (58.0, (0xBC, 0x74, 0x31)),
    (65.99, (0xBC, 0x74, 0x31)),
    (66.0, (0x9A, 0x4E, 0x17)),
    (73.99, (0x9A, 0x4E, 0x17)),
    (74.0, (0x6E, 0x34, 0x10)),
    (95.0, (0x6E, 0x34, 0x10)),
)


def decode(png: bytes):
    return Image.open(io.BytesIO(png))


# --- palette ------------------------------------------------------------------------


def test_the_ramp_is_the_five_heat_tokens():
    assert HEAT_COLORS == ("#C9B896", "#C79B5B", "#BC7431", "#9A4E17", "#6E3410")


def test_the_palette_reserves_index_zero_and_holds_the_ramp():
    table = palette_bytes()
    assert len(table) == PALETTE_ENTRIES * 3
    assert table[0:3] == b"\x00\x00\x00"
    assert table[3:6] == bytes((0xC9, 0xB8, 0x96))
    assert table[15:18] == bytes((0x6E, 0x34, 0x10))


@pytest.mark.parametrize("celsius,expected_index", [
    (40.0, 1), (49.99, 1), (50.0, 2), (57.99, 2), (58.0, 3),
    (65.99, 3), (66.0, 4), (73.99, 4), (74.0, 5), (150.0, 5),
])
def test_band_edges_land_on_the_documented_step(celsius, expected_index):
    tile = np.array([[round(celsius * 100)]], dtype=np.int16)
    assert int(palette_indices(tile)[0, 0]) == expected_index


def test_unobserved_pixels_get_the_transparent_index():
    tile = np.array([[EMPTY_CENTI_C, 4500]], dtype=np.int16)
    assert palette_indices(tile).tolist() == [[TRANSPARENT_INDEX, 1]]


# --- encoding -----------------------------------------------------------------------


def test_every_band_decodes_to_its_token_colour():
    tile = np.full((1, len(EXPECTED_RAMP)), EMPTY_CENTI_C, dtype=np.int16)
    for column, (celsius, _) in enumerate(EXPECTED_RAMP):
        tile[0, column] = round(celsius * 100)

    decoded = decode(encode_tile_png(tile)).convert("RGBA")

    for column, (_, expected_rgb) in enumerate(EXPECTED_RAMP):
        assert decoded.getpixel((column, 0)) == (*expected_rgb, 255)


def test_unobserved_pixels_decode_as_fully_transparent():
    tile = np.full((2, 2), EMPTY_CENTI_C, dtype=np.int16)
    tile[1, 1] = 4200

    image = decode(encode_tile_png(tile))
    assert image.mode == "P"
    assert image.info["transparency"] == TRANSPARENT_INDEX

    decoded = image.convert("RGBA")
    assert decoded.getpixel((0, 0))[3] == 0
    assert decoded.getpixel((1, 0))[3] == 0
    assert decoded.getpixel((1, 1)) == (0xC9, 0xB8, 0x96, 255)


def test_a_full_tile_round_trips_at_the_right_size():
    tile = blank_tile()
    tile[0, 0] = 8000
    tile[255, 255] = 4000

    image = decode(encode_tile_png(tile))
    assert image.size == (256, 256)

    decoded = image.convert("RGBA")
    assert decoded.getpixel((0, 0)) == (0x6E, 0x34, 0x10, 255)
    assert decoded.getpixel((255, 255)) == (0xC9, 0xB8, 0x96, 255)
    assert decoded.getpixel((128, 128))[3] == 0


def test_a_sparse_palette_survives_a_round_trip():
    """Index values, not just colours, must come back unchanged.

    The all-time pyramid merges parent tiles by comparing palette ranks, so an
    encoder that renumbered a sparse index set -- exactly the temptation
    ``optimize=True`` creates -- would silently corrupt the archive's coarse
    zooms while every colour still looked right.
    """
    tile = blank_tile()
    tile[0, 0] = 8000  # rank 5
    tile[0, 1] = 6000  # rank 3
    tile[5, 5] = 4100  # rank 1, with ranks 2 and 4 absent entirely

    expected = palette_indices(tile)
    decoded = decode_indices_png(encode_tile_png(tile))

    assert sorted(set(expected.ravel().tolist())) == [0, 1, 3, 5]
    assert np.array_equal(decoded, expected)


def test_rank_arrays_encode_and_decode_directly():
    ranks = np.zeros((4, 4), dtype=np.uint8)
    ranks[1, 2] = 4
    assert np.array_equal(decode_indices_png(encode_indices_png(ranks)), ranks)


def test_rows_and_columns_are_not_transposed():
    tile = np.full((4, 4), EMPTY_CENTI_C, dtype=np.int16)
    tile[0, 3] = 8000  # first row, last column

    decoded = decode(encode_tile_png(tile)).convert("RGBA")

    # Pillow indexes (x, y), numpy indexes (row, column).
    assert decoded.getpixel((3, 0))[3] == 255
    assert decoded.getpixel((0, 3))[3] == 0
