"""A minimal PNG encoder and an integer-only drawing surface.

Written rather than pulled in because of what the alternative costs. Pillow is ~10 MB of
wheel and a stack of C codecs, added to an API image so it can draw rectangles and
one-pixel lines — this module is the entire subset actually needed, in stdlib `zlib`, with
no native build step and nothing to keep patched.

PNG is genuinely small: an 8-byte signature, an IHDR describing the raster, IDAT holding
zlib-compressed scanlines each prefixed with a filter byte, and an IEND. Truecolour at 8
bits with filter 0 on every row is the simplest legal encoding and is what browsers open
fastest.

**Everything here is integer arithmetic on a byte buffer.** No floats, matching the rest
of the codebase — a pixel coordinate computed as `0.1 + 0.2` and truncated lands one row
off, which in a candle chart is the difference between a wick touching a level and not.
Callers scale their own values to pixels with `Decimal` and hand over `int`.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

#: R, G, B. Alpha is deliberately absent: charts here are opaque, and dropping the channel
#: makes every buffer a quarter smaller and every blend unnecessary.
Colour = tuple[int, int, int]


def encode_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode an RGB buffer as a PNG.

    ``pixels`` is ``width * height * 3`` bytes, row-major, no padding.
    """
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} bytes for {width}x{height}, got {len(pixels)}")

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    # Filter byte 0 ("None") in front of every scanline. Adaptive filtering would compress
    # a photograph better; on flat-coloured chart output it saves almost nothing and makes
    # the encoder several times longer.
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += pixels[row * stride : (row + 1) * stride]

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


@dataclass
class Canvas:
    """A mutable RGB raster with the handful of primitives a candle chart needs.

    Every method clips silently rather than raising. A wick one pixel past the top of the
    plot area is a rounding artefact, not a caller error, and a renderer that raised on it
    would fail on the first trade whose high sat exactly at the window's maximum.
    """

    width: int
    height: int
    pixels: bytearray

    @classmethod
    def filled(cls, width: int, height: int, colour: Colour) -> Canvas:
        return cls(width, height, bytearray(bytes(colour) * (width * height)))

    def set(self, x: int, y: int, colour: Colour) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(colour)

    def rect(self, x: int, y: int, w: int, h: int, colour: Colour) -> None:
        if w <= 0 or h <= 0:
            return
        left, right = max(x, 0), min(x + w, self.width)
        top, bottom = max(y, 0), min(y + h, self.height)
        if left >= right or top >= bottom:
            return
        row = bytes(colour) * (right - left)
        for py in range(top, bottom):
            start = (py * self.width + left) * 3
            self.pixels[start : start + len(row)] = row

    def vline(self, x: int, y0: int, y1: int, colour: Colour) -> None:
        self.rect(x, min(y0, y1), 1, abs(y1 - y0) + 1, colour)

    def hline(self, y: int, x0: int, x1: int, colour: Colour) -> None:
        self.rect(min(x0, x1), y, abs(x1 - x0) + 1, 1, colour)

    def dashed_hline(
        self, y: int, x0: int, x1: int, colour: Colour, *, on: int = 4, off: int = 4
    ) -> None:
        """A stop or target level. Dashed so it reads as a line the trader placed."""
        x = min(x0, x1)
        end = max(x0, x1)
        while x <= end:
            self.hline(y, x, min(x + on - 1, end), colour)
            x += on + off

    def to_png(self) -> bytes:
        return encode_png(self.width, self.height, bytes(self.pixels))


#: A 5x7 bitmap font, one entry per column, bit 0 = top row.
#:
#: Only the glyphs price and time labels need. A full ASCII face would be ~95 entries of
#: hand-authored bitmap for no gain: nothing on a generated chart spells words, and the
#: label text is produced by this module rather than supplied by a caller.
_GLYPHS: dict[str, tuple[int, ...]] = {
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E),
    "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x62, 0x51, 0x49, 0x49, 0x46),
    "3": (0x22, 0x41, 0x49, 0x49, 0x36),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10),
    "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30),
    "7": (0x01, 0x71, 0x09, 0x05, 0x03),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36),
    "9": (0x06, 0x49, 0x49, 0x29, 0x1E),
    ".": (0x00, 0x60, 0x60, 0x00, 0x00),
    ":": (0x00, 0x36, 0x36, 0x00, 0x00),
    "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    " ": (0x00, 0x00, 0x00, 0x00, 0x00),
}

GLYPH_WIDTH = 5
GLYPH_HEIGHT = 7
GLYPH_SPACING = 1


def text_width(text: str) -> int:
    return len(text) * (GLYPH_WIDTH + GLYPH_SPACING) - GLYPH_SPACING if text else 0


def draw_text(canvas: Canvas, x: int, y: int, text: str, colour: Colour) -> None:
    """Draw a label. Characters with no glyph are skipped, never substituted.

    Substituting a box or a question mark would put a character on a chart that the data
    does not contain — small, but the same species of invention this codebase refuses
    everywhere else.
    """
    cursor = x
    for character in text:
        columns = _GLYPHS.get(character)
        if columns is not None:
            for column, bits in enumerate(columns):
                for row in range(GLYPH_HEIGHT):
                    if bits & (1 << row):
                        canvas.set(cursor + column, y + row, colour)
        cursor += GLYPH_WIDTH + GLYPH_SPACING
