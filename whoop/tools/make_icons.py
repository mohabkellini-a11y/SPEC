#!/usr/bin/env python3
"""Generate the PWA icons locally. No image library, no downloads.

iOS ignores the manifest's icons for the home screen and uses `apple-touch-icon`
instead, and it will not use an SVG there — so a real PNG has to exist. Rather
than pull in Pillow, this writes minimal uncompressed-ish PNGs by hand: a dark
rounded square with the recovery ring, drawn pixel by pixel.

    python tools/make_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web" / "icons"

BG = (11, 15, 14)
RING = (24, 201, 139)
TRACK = (28, 36, 34)


def png(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    """Encode RGB pixels as a PNG."""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("3B", *pixel) for pixel in row)
        for row in pixels
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def blend(under: tuple[int, int, int], over: tuple[int, int, int],
          alpha: float) -> tuple[int, int, int]:
    alpha = max(0.0, min(1.0, alpha))
    return tuple(round(u + (o - u) * alpha) for u in (under,) for u, o in zip(under, over))


def draw(size: int, maskable: bool = False) -> list[list[tuple[int, int, int]]]:
    """A dark tile with the recovery ring, matching the app's favicon."""
    centre = (size - 1) / 2.0
    # A maskable icon must survive being cropped to a circle, so keep the art
    # inside the safe zone (80% of the width).
    scale = 0.62 if maskable else 0.78
    radius = size * scale / 2.0
    thickness = max(2.0, size * (0.10 if maskable else 0.12))
    corner = size * 0.22

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            colour = BG
            # rounded-square background
            dx = max(abs(x - centre) - (size / 2 - corner), 0)
            dy = max(abs(y - centre) - (size / 2 - corner), 0)
            if not maskable and math.hypot(dx, dy) > corner:
                row.append((0, 0, 0))
                continue

            dist = math.hypot(x - centre, y - centre)
            edge = abs(dist - radius)
            if edge <= thickness / 2:
                angle = math.degrees(math.atan2(centre - y, x - centre))
                angle = (90 - angle) % 360          # clockwise from 12 o'clock
                lit = angle <= 268                  # ~75% arc, like the favicon
                target = RING if lit else TRACK
                # feather the edge so it does not look jagged at 180px
                alpha = min(1.0, (thickness / 2 - edge) / max(1.0, size * 0.01))
                colour = blend(BG, target, alpha)
            row.append(colour)
        rows.append(row)
    return rows


def main() -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    made = []
    for size in (180, 192, 512):
        path = WEB / f"icon-{size}.png"
        path.write_bytes(png(size, size, draw(size)))
        made.append((path.name, path.stat().st_size))
    for size in (192, 512):
        path = WEB / f"icon-{size}-maskable.png"
        path.write_bytes(png(size, size, draw(size, maskable=True)))
        made.append((path.name, path.stat().st_size))

    for name, size in made:
        print(f"  {name:<26} {size / 1024:6.1f} KB")
    print(f"\nWrote {len(made)} icons to {WEB}")
    print("180px is the apple-touch-icon iOS uses for the home screen.")


if __name__ == "__main__":
    main()
