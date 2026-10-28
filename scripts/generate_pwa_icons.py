#!/usr/bin/env python3
"""Generate KwentoFi PWA, favicon, and compact mark assets from approved art.

Source artwork lives in ``static/branding/kwentofi/``. The PWA source is a
square, text-free app mark. Maskable outputs scale the art into the central
safe circle; ordinary outputs retain the full designed canvas.

Usage:
    python scripts/generate_pwa_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "static" / "branding" / "kwentofi"
ICON_DIR = ROOT / "static" / "icons"
PWA_SOURCE = BRAND_DIR / "kwentofi-pwa-icon-source.png"
FAVICON_SOURCE = BRAND_DIR / "kwentofi-favicon-source.png"
SIZES = (512, 192)
MASKABLE_SCALE = 0.84


def _load_square(path: Path) -> Image.Image:
    image = Image.open(path).convert("RGB")
    if image.width != image.height:
        raise ValueError(f"source must be square, got {image.width}x{image.height}: {path}")
    return image


def _resize(source: Image.Image, size: int) -> Image.Image:
    return ImageOps.fit(
        source,
        (size, size),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _maskable(source: Image.Image, size: int) -> Image.Image:
    """Keep the mark within the central 80% Android adaptive-icon safe area."""
    background = source.getpixel((0, 0))
    inner_size = round(size * MASKABLE_SCALE)
    artwork = _resize(source, inner_size)
    canvas = Image.new("RGB", (size, size), background)
    offset = ((size - inner_size) // 2, (size - inner_size) // 2)
    canvas.paste(artwork, offset)
    return canvas


def main() -> int:
    missing = [path for path in (PWA_SOURCE, FAVICON_SOURCE) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"missing KwentoFi source artwork: {path}", file=sys.stderr)
        return 1

    try:
        pwa_source = _load_square(PWA_SOURCE)
        favicon_source = _load_square(FAVICON_SOURCE)
    except Exception as exc:
        print(f"could not load KwentoFi source artwork: {exc}", file=sys.stderr)
        return 1

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)

    for size in SIZES:
        for purpose in ("any", "maskable"):
            name = (
                f"kwentofi-pwa-{size}.png"
                if purpose == "any"
                else f"kwentofi-pwa-maskable-{size}.png"
            )
            image = _resize(pwa_source, size) if purpose == "any" else _maskable(pwa_source, size)
            output = ICON_DIR / name
            image.save(output, "PNG", optimize=True)
            print(f"wrote {output.relative_to(ROOT)} ({size}x{size}, {purpose})")

    favicon32 = _resize(favicon_source, 32)
    favicon32.save(ICON_DIR / "kwentofi-favicon-32.png", "PNG", optimize=True)
    favicon48 = _resize(favicon_source, 48)
    favicon48.save(ICON_DIR / "kwentofi-favicon-48.png", "PNG", optimize=True)
    ico_master = _resize(favicon_source, 48)
    ico_master.save(
        ICON_DIR / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )

    touch = _resize(pwa_source, 180)
    touch.save(ICON_DIR / "kwentofi-apple-touch-180.png", "PNG", optimize=True)
    symbol = _resize(favicon_source, 64)
    symbol.save(BRAND_DIR / "kwentofi-symbol-64.png", "PNG", optimize=True)
    print(f"wrote {ICON_DIR.relative_to(ROOT)}/favicon.ico (16, 32, 48)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
