#!/usr/bin/env python3
"""Generate icon PNGs for the cc-quota-fetcher Chrome extension.

Design: a rounded blue chip with a white 270° progress arc.

Each size is drawn independently (no scaling) to keep small sizes crisp.
Output: icon-16.png, icon-32.png, icon-48.png, icon-128.png in this dir.

Run after editing the design. Requires Pillow:
  ~/.claude/scripts/venv/bin/python3 -m pip install Pillow
  ~/.claude/scripts/venv/bin/python3 extension/icons/generate.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent
SIZES = (16, 32, 48, 128)

CHIP_COLOR = (91, 141, 239, 255)
RING_COLOR = (255, 255, 255, 255)


def draw_icon(size: int) -> Image.Image:
    scale = 4
    canvas = size * scale
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    chip_pad = max(scale, canvas // 16)
    draw.ellipse(
        [chip_pad, chip_pad, canvas - chip_pad, canvas - chip_pad],
        fill=CHIP_COLOR,
    )

    ring_pad = chip_pad + canvas // 7
    ring_box = [ring_pad, ring_pad, canvas - ring_pad, canvas - ring_pad]
    ring_thickness = max(scale * 2, canvas // 9)

    draw.arc(ring_box, start=135, end=45, fill=RING_COLOR, width=ring_thickness)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        out = OUT_DIR / f"icon-{size}.png"
        draw_icon(size).save(out)
        print(f"wrote {out.relative_to(OUT_DIR.parent.parent)} ({size}x{size})")


if __name__ == "__main__":
    main()
