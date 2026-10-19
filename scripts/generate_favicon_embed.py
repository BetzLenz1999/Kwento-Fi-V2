#!/usr/bin/env python3
"""Regenerate `templates/_favicon.html` — the EMBEDDED GoodMarket favicon.

The logo is inlined as base64 PNG data URIs, so the partial is fully
self-contained: copy-paste it into any repo and the icon works with no
`/static` asset and no `.ico`/`.png` file to carry along.

Usage:
    python scripts/generate_favicon_embed.py [image-url-or-path]

Requires Pillow (`pip install pillow`). The source is expected to be a square
logo on a white background. The white is FLOOD-FILLED from the border (not
thresholded) so internal white details of the emblem survive, then the emblem
is inset by a small margin so it is not clipped at the tab edge.
"""
import argparse
import base64
import io
import sys
import urllib.request
from collections import deque
from string import Template

DEFAULT_SOURCE = "https://i.ibb.co/MDrjRczQ/photo-6262591230365601278-c.jpg"
MARGIN_PCT = 0.02
WHITE_CUTOFF = 232  # channel value above which a border-connected pixel is "background"

# (label, pixel size, palette colours, opaque?) — small sizes for the tab,
# a 180px opaque one for the iOS home screen (iOS composites alpha onto black).
VARIANTS = {
    "icon16": (16, 64, False),
    "icon32": (32, 128, False),
    "apple180": (180, 32, True),
}

# Every size is inlined so the partial is fully self-contained: copy-paste it
# into any repo (Jinja OR plain HTML) and the icon works with no asset files.
INLINE_KEYS = ("icon16", "icon32", "apple180")


def load_source(src):
    from PIL import Image
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return Image.open(io.BytesIO(resp.read()))
    return Image.open(src)


def build_emblem(im, margin_pct=MARGIN_PCT):
    """Return the logo as RGBA with the surrounding white made transparent.

    A border flood fill is used instead of a global threshold: the emblem has
    white line-work inside it, and thresholding would punch holes in it.
    """
    from PIL import Image
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    mask = Image.new("L", (w, h), 255)
    mp = mask.load()
    seen = bytearray(w * h)
    dq = deque()

    def is_bg(x, y):
        r, g, b = px[x, y]
        return r > WHITE_CUTOFF and g > WHITE_CUTOFF and b > WHITE_CUTOFF

    def push(x, y):
        if not seen[y * w + x] and is_bg(x, y):
            seen[y * w + x] = 1
            dq.append((x, y))

    for x in range(w):
        push(x, 0)
        push(x, h - 1)
    for y in range(h):
        push(0, y)
        push(w - 1, y)

    while dq:
        x, y = dq.popleft()
        mp[x, y] = 0
        if x > 0:
            push(x - 1, y)
        if x < w - 1:
            push(x + 1, y)
        if y > 0:
            push(x, y - 1)
        if y < h - 1:
            push(x, y + 1)

    out = im.convert("RGBA")
    out.putalpha(mask)
    out = out.crop(out.getbbox())

    s = out.size[0]
    m = max(1, int(s * margin_pct))
    canvas = Image.new("RGBA", (s + 2 * m, s + 2 * m), (255, 255, 255, 0))
    canvas.paste(out, (m, m), out)
    return canvas


def png_bytes(im, size, colors, opaque):
    from PIL import Image
    im = im.resize((size, size), Image.LANCZOS)
    if opaque:
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg.convert("RGB")
    im = im.quantize(colors=colors, method=Image.FASTOCTREE)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def ico_bytes(emblem, sizes=(16, 32, 48)):
    """Build a multi-size .ico whose entries are PNG-compressed.

    A real .ico (not just a PNG served as image/x-icon) is what old browsers and
    Windows taskbar pins expect from the root /favicon.ico.
    """
    from PIL import Image
    images = []
    for size in sizes:
        im = emblem.resize((size, size), Image.LANCZOS).convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        images.append((size, buf.getvalue()))

    header = bytearray()
    header += (0).to_bytes(2, "little")          # reserved
    header += (1).to_bytes(2, "little")          # type: 1 = icon
    header += len(images).to_bytes(2, "little")  # image count
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += bytes([dim, dim, 0, 0])
        entries += (1).to_bytes(2, "little")      # color planes
        entries += (32).to_bytes(2, "little")     # bits per pixel
        entries += len(data).to_bytes(4, "little")
        entries += offset.to_bytes(4, "little")
        offset += len(data)
    body = b"".join(data for _, data in images)
    return bytes(header) + bytes(entries) + body


PY_MODULE = '''"""GoodMarket app icon bytes — EMBEDDED (generated, do not edit by hand).

The logo lives here as base64 so the root `/favicon.ico` and
`/apple-touch-icon.png` routes serve it with NO icon file on disk. Regenerate:

    python scripts/generate_favicon_embed.py <image-url-or-path>

The same artwork is inlined into every page <head> via `templates/_favicon.html`.
"""
import base64

ICON_PNG_16 = base64.b64decode(
    "$icon16"
)

ICON_PNG_32 = base64.b64decode(
    "$icon32"
)

APPLE_TOUCH_PNG_180 = base64.b64decode(
    "$apple180"
)

FAVICON_ICO = base64.b64decode(
    "$favicon_ico"
)


def png_bytes(name):
    """Return the raw PNG bytes for a named icon, or None if unknown."""
    return {
        "icon-16": ICON_PNG_16,
        "icon-32": ICON_PNG_32,
        "apple-touch-180": APPLE_TOUCH_PNG_180,
    }.get(name)


def ico_bytes():
    """Return the multi-size .ico bytes."""
    return FAVICON_ICO
'''



HEADER = """<!-- GoodMarket favicon / app icon — EMBEDDED (base64 data URI).

     The logo is inlined here so this file is fully self-contained: copy-paste it
     into any repo (Jinja or plain HTML) and the icon works with NO /static asset
     and no .ico/.png file to carry along. Regenerate with:
         python scripts/generate_favicon_embed.py <image-url-or-path>

     The root /favicon.ico + /apple-touch-icon.png routes serve these same bytes
     (see favicon_embed.py) for browsers and link previewers that request a real
     URL instead of reading a data-URI <link>. -->
"""

BODY = """<link rel="icon" type="image/png" sizes="16x16" href="data:image/png;base64,{icon16}">
<link rel="icon" type="image/png" sizes="32x32" href="data:image/png;base64,{icon32}">
<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,{apple180}">
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", default=DEFAULT_SOURCE,
                    help="image URL or local path (default: the GoodMarket logo)")
    ap.add_argument("--out", default=None, help="output path (default: templates/_favicon.html)")
    ap.add_argument("--module", default=None, help="output path for the Python module (default: favicon_embed.py)")
    args = ap.parse_args(argv)

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        sys.exit("Pillow is required: pip install pillow")

    here = __file__
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(here)))
    out_path = args.out or os.path.join(root, "templates", "_favicon.html")

    emblem = build_emblem(load_source(args.source))
    data = {}
    for key, (size, colors, opaque) in VARIANTS.items():
        raw = png_bytes(emblem, size, colors, opaque)
        data[key] = base64.b64encode(raw).decode()
        print(f"  {key:9s} {size:3d}px c{colors:<3d} b64={len(data[key]):6d} chars")

    ico = ico_bytes(emblem)
    data["favicon_ico"] = base64.b64encode(ico).decode()
    print(f"  favicon.ico 16/32/48      b64={len(data['favicon_ico']):6d} chars")

    html = HEADER + BODY.format(**data)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"wrote {out_path} ({len(html)} bytes)")

    module_path = args.module or os.path.join(root, "favicon_embed.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(Template(PY_MODULE).substitute(**data))
    print(f"wrote {module_path}")


if __name__ == "__main__":
    main()
