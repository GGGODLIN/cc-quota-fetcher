#!/usr/bin/env python3
"""Rasterize icon.svg into icon-{16,32,48,128}.png.

Edit extension/icons/icon.svg (any vector tool — Inkscape, Figma export,
hand-edited XML — works) then run this script to regenerate the PNGs.

Requires `rsvg-convert` from librsvg:
  brew install librsvg
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
SVG = OUT_DIR / "icon.svg"
SIZES = (16, 32, 48, 128)


def main() -> int:
    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        print("rsvg-convert not found. Install with: brew install librsvg", file=sys.stderr)
        return 1
    if not SVG.is_file():
        print(f"missing {SVG}", file=sys.stderr)
        return 2

    for size in SIZES:
        out = OUT_DIR / f"icon-{size}.png"
        subprocess.run(
            [rsvg, "-w", str(size), "-h", str(size), "-o", str(out), str(SVG)],
            check=True,
        )
        print(f"wrote {out.relative_to(OUT_DIR.parent.parent)} ({size}x{size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
