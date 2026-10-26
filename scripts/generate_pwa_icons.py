#!/usr/bin/env python3
"""Regenerate the installable-app (PWA) icon PNGs from the GoodMarket SVG.

Why this exists
---------------
`static/manifest.json` advertises the same 512px PNG for both `purpose: any`
and `purpose: maskable`. Maskable icons are cropped by Android to the inner
80% circle, and the shipped artwork fills its canvas edge to edge (content
reaches 51.4% of the width from centre against a 40% safe radius), so Android
was cutting ~22% of the mark off. An installable app must not ship a maskable
icon whose content falls outside the safe zone.

This script renders the editable SVG source of truth (the same artwork the web
favicon uses) and composites it into the safe circle on a solid canvas, so the
crop can never clip it.

Usage
-----
    pip install cairosvg pillow
    python scripts/generate_pwa_icons.py

Outputs (overwritten in place):
    static/icons/icon-maskable-512.png
    static/icons/icon-maskable-192.png

The `purpose: any` icons (`goodmarket-icon.png`, `icon-192x192.png`) are NOT
touched: they are not cropped, so full-bleed artwork is correct there.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "static" / "branding" / "goodmarket" / "goodmarket-icon.svg"
OUT_DIR = ROOT / "static" / "icons"

# Android/maskable: content must sit inside the inner 80% circle (radius = 40%
# of the canvas). The SVG's own outer disc is r=310 of a 640 viewBox, so a
# rendered canvas of scale S puts the disc edge at 0.484*S of the icon width.
# 0.76 -> ~0.368, comfortably inside the 0.40 safe radius (and inside the
# stricter rounded-rect / squircle masks too).
SAFE_SCALE = 0.76

# Match manifest.json "background_color" so the OS splash and the icon blend
# into one another instead of flashing two different colours.
BACKGROUND = (247, 241, 232)  # #f7f1e8

# cairosvg ignores <style> class rules (a documented gotcha in this repo), so
# the classes are resolved into presentation attributes before rendering.
_CLASS_STYLES = {
    "ink": {"fill": "#102a43"},
    "blue": {
        "fill": "none",
        "stroke": "#18a9df",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
    },
    "label": {
        "font-family": "Arial, Helvetica, sans-serif",
        "font-weight": "800",
        "text-anchor": "middle",
    },
}


def _inline_styles(svg: str) -> str:
    """Resolve class="..." into inline style="..." attributes."""

    def add_props(match: re.Match) -> str:
        tag = match.group(0)
        class_match = re.search(r'class="([^"]+)"', tag)
        if not class_match:
            return tag
        props: dict[str, str] = {}
        for name in class_match.group(1).split():
            props.update(_CLASS_STYLES.get(name, {}))
        # Never override an attribute the element already sets explicitly.
        keep = {
            k: v
            for k, v in props.items()
            if not re.search(r"\b%s\s*=" % re.escape(k), tag)
        }
        if not keep:
            return tag
        style = ";".join(f"{k}:{v}" for k, v in keep.items())
        if tag.endswith("/>"):
            return tag[:-2] + f' style="{style}"/>'
        return tag[:-1] + f' style="{style}">'

    inlined = re.sub(r"<[^>]+>", add_props, svg)
    return re.sub(r"<style>.*?</style>", "", inlined, flags=re.S)


def build_icon(size: int):
    """Return a size x size RGB image with the mark inside the safe circle.

    The SVG is rendered with a TRANSPARENT background on purpose. Rendering it
    on white produces an opaque square whose corners sit 0.707*inner from the
    centre — outside the 0.40 safe radius even though the disc itself is small.
    With transparency the only opaque pixels are the artwork's own disc
    (r=310 of a 640 viewBox = 0.484 of the rendered canvas), so the safe-zone
    check measures the mark, not a background plate.
    """
    from PIL import Image

    svg = _inline_styles(SVG.read_text(encoding="utf-8"))
    inner = int(round(size * SAFE_SCALE))
    png = cairosvg.svg2png(  # noqa: F821 - imported in main() with a clear error
        bytestring=svg.encode("utf-8"),
        output_width=inner,
        output_height=inner,
    )
    mark = Image.open(io.BytesIO(png)).convert("RGBA")

    canvas = Image.new("RGB", (size, size), BACKGROUND)
    offset = (size - inner) // 2
    canvas.paste(mark, (offset, offset), mark)
    return canvas


def main() -> int:
    global cairosvg
    try:
        import cairosvg  # noqa: PLC0415
    except ImportError:
        print(
            "cairosvg is required: pip install cairosvg pillow",
            file=sys.stderr,
        )
        return 1

    if not SVG.is_file():
        print(f"missing brand source: {SVG}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (512, 192):
        out = OUT_DIR / f"icon-maskable-{size}.png"
        build_icon(size).save(out, "PNG", optimize=True)
        print(f"wrote {out.relative_to(ROOT)} ({size}x{size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
