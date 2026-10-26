#!/usr/bin/env python3
"""Generate the PWA PNG icon set from the approved GoodMarket-only artwork.

The editable source is the AI-generated, text-free mark at
`static/branding/goodmarket/goodmarket-only-app-icon.png`. It contains no G$,
lettering, or currency symbols. This script exports distinct 192px and 512px
icons for both ordinary and Android maskable use.

Usage:
    python scripts/generate_pwa_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "static" / "branding" / "goodmarket" / "goodmarket-only-app-icon.png"
OUT_DIR = ROOT / "static" / "icons"
BACKGROUND = (247, 241, 232)  # #f7f1e8, matching manifest background_color
SIZES = (512, 192)


def _load_source() -> Image.Image:
    """Load the square source art as RGB and fail clearly on malformed input."""
    image = Image.open(SOURCE).convert("RGB")
    if image.width != image.height:
        raise ValueError(f"source icon must be square, got {image.width}x{image.height}")
    return image


def _render(source: Image.Image, size: int, *, maskable: bool) -> Image.Image:
    """Resize artwork and return a solid-background square PNG image.

    The generated source keeps the market mark inside the Android adaptive-icon
    safe circle. Ordinary and maskable files are written separately so the
    manifest can declare their intended purposes without reusing one URL.
    """
    resampling = Image.Resampling.LANCZOS
    artwork = ImageOps.fit(source, (size, size), method=resampling, centering=(0.5, 0.5))
    if maskable:
        # The source artwork is already composed for the central safe circle;
        # retain its full designed scale instead of shrinking it a second time.
        return artwork
    return artwork


def main() -> int:
    if not SOURCE.is_file():
        print(f"missing GoodMarket-only icon source: {SOURCE}", file=sys.stderr)
        return 1

    try:
        source = _load_source()
    except Exception as exc:
        print(f"could not load GoodMarket-only icon source: {exc}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        for purpose in ("any", "maskable"):
            suffix = "" if purpose == "any" else "-maskable"
            output = OUT_DIR / f"goodmarket-only{suffix}-{size}.png"
            _render(source, size, maskable=(purpose == "maskable")).save(
                output, "PNG", optimize=True
            )
            print(f"wrote {output.relative_to(ROOT)} ({size}x{size}, {purpose})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
