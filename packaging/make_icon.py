"""Draw packaging/wardmind.ico: the top bar's document mark, white on the plum accent.

Run it again after changing the mark:  uv run python packaging/make_icon.py
It needs nothing beyond the standard library. Each size is drawn on its own (with 4x4
supersampling) and stored as a PNG inside the .ico, which Windows Vista and later read.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).with_name("wardmind.ico")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PLUM = (0x6A, 0x4B, 0xD6)
WHITE = (0xFF, 0xFF, 0xFF)
SAMPLES = 4  # per axis

# The `brand` symbol in web/static/icons.svg, in its 16x16 viewBox.
OUTLINE = [(3, 2.5), (10.5, 2.5), (13, 5), (13, 13.5), (3, 13.5)]
LINES = [((5.5, 7), (10.5, 7)), ((5.5, 9.5), (10.5, 9.5)), ((5.5, 12), (8.5, 12))]


def _segments() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    closed = [(OUTLINE[i], OUTLINE[(i + 1) % len(OUTLINE)]) for i in range(len(OUTLINE))]
    return closed + LINES


def _distance(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _inside_rounded_square(x: float, y: float, size: int, radius: float) -> bool:
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def draw(size: int) -> bytes:
    """RGBA pixels, row by row."""
    segments = _segments()
    # The mark fills ~60% of the tile; strokes thicken a little at small sizes.
    scale = size * 0.6 / 11.0  # the mark spans 10 x 11 units
    offset_x = (size - 10 * scale) / 2 - 3 * scale
    offset_y = (size - 11 * scale) / 2 - 2.5 * scale
    stroke = (1.7 if size <= 32 else 1.25) * scale
    radius = size * 0.22
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter: none
        for x in range(size):
            tile = ink = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    fx = x + (sx + 0.5) / SAMPLES
                    fy = y + (sy + 0.5) / SAMPLES
                    if not _inside_rounded_square(fx, fy, size, radius):
                        continue
                    tile += 1
                    ux, uy = (fx - offset_x) / scale, (fy - offset_y) / scale
                    if min(_distance(ux, uy, a, b) for a, b in segments) * scale <= stroke / 2:
                        ink += 1
            total = SAMPLES * SAMPLES
            if tile == 0:
                rows += bytes(4)
                continue
            mix = ink / tile
            rgb = (round(PLUM[i] * (1 - mix) + WHITE[i] * mix) for i in range(3))
            rows += bytes((*rgb, round(255 * tile / total)))
    return bytes(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(draw(size), 9))
        + chunk(b"IEND", b"")
    )


def ico(images: dict[int, bytes]) -> bytes:
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in images.items():
        edge = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", edge, edge, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return directory + entries + b"".join(images.values())


if __name__ == "__main__":
    OUT.write_bytes(ico({size: png(size) for size in SIZES}))
    print(f"Wrote {OUT}")
