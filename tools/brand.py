#!/usr/bin/env python3
"""Brand assets from one geometry: SVG marks, favicon, wordmark, social card.

    python3 -m venv /tmp/iconenv && /tmp/iconenv/bin/pip install Pillow fonttools brotli
    /tmp/iconenv/bin/python tools/brand.py

Writes assets/brand/{mark,mark-mono,mark-plate,wordmark,wordmark-dark,favicon}.svg
and docs/social-preview.png. The mark's geometry lives in macos/make-icon.py
(which also builds the app icon), so there is exactly one drawing of it.

The wordmark is outlined from the bundled Space Grotesk, not set as <text>:
GitHub renders README images without web fonts, and a wordmark that falls
back to Arial is not a wordmark.
"""
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"

spec = importlib.util.spec_from_file_location("make_icon", ROOT / "macos" / "make-icon.py")
icon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(icon)

ULTRA, INK, PAPER, MARKER = "#203AD2", "#111826", "#F4F7FB", "#DEF766"
ULTRA_ON_DARK = "#A4B4FF"


def rects(shapes, colours, dx=0.0, dy=0.0, k=1.0):
    out = []
    for x, y, w, h, r, role in shapes:
        fill = colours[role]
        out.append(f'<rect x="{dx + x * k:.2f}" y="{dy + y * k:.2f}" width="{w * k:.2f}" '
                   f'height="{h * k:.2f}" rx="{r * k:.2f}" fill="{fill}"/>')
    return "".join(out)


def plate(size, dx=0.0, dy=0.0, small=False):
    """The mark on its ultramarine plate, as used for favicon and lockup."""
    shapes = icon.MARK_SMALL if small else icon.MARK
    x0, y0, x1, y1 = icon.bbox(shapes)
    k = size * (0.66 if small else 0.58) / max(x1 - x0, y1 - y0)
    ox = dx + size / 2 - (x0 + x1) / 2 * k
    oy = dy + size / 2 - (y0 + y1) / 2 * k + size * 0.02
    r = size * 0.235
    return (f'<rect x="{dx}" y="{dy}" width="{size}" height="{size}" rx="{r:.2f}" fill="{ULTRA}"/>'
            + rects(shapes, {"text": PAPER, "sound": PAPER, "cursor": MARKER}, ox, oy, k))


def svg(w, h, body, label="Fundus"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" '
            f'height="{h:g}" role="img" aria-label="{label}">{body}</svg>\n')


def outline(text, font_path, cap_px, tracking=-0.02):
    """Glyph outlines as one SVG path, scaled so cap height = cap_px.
    Returns (path_d, width_px). Kerning from the font's kern pairs is not
    applied; at -2 % tracking 'Fundus' has no pair that needs it."""
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    f = TTFont(font_path)
    gs = f.getGlyphSet()
    cmap = f.getBestCmap()
    cap = getattr(f["OS/2"], "sCapHeight", 0) or 700
    upm = f["head"].unitsPerEm
    k = cap_px / cap
    pen = SVGPathPen(gs)
    x = 0.0
    for ch in text:
        g = cmap[ord(ch)]
        tp = TransformPen(pen, (k, 0, 0, -k, x, 0))
        gs[g].draw(tp)
        x += gs[g].width * k + tracking * upm * k
    return pen.getCommands(), x - tracking * upm * k


def wordmark(ink):
    """Lockup: plate + name. Height 64, cap height of the name 30."""
    size = 64
    d, tw = outline("Fundus", ROOT / "fonts" / "space-grotesk-latin-700-normal.woff2", 30)
    gap = 18
    base = size / 2 + 15  # baseline: caps centred on the plate
    w = size + gap + tw + 2
    body = plate(size) + f'<path transform="translate({size + gap} {base})" d="{d}" fill="{ink}"/>'
    return svg(round(w, 1), size, body)


def write_svgs():
    BRAND.mkdir(parents=True, exist_ok=True)
    x0, y0, x1, y1 = icon.bbox(icon.MARK)
    # Square artboard around the mark with the same air on every side.
    pad = 4
    side = max(x1 - x0, y1 - y0) + 2 * pad
    dx = (side - (x1 - x0)) / 2 - x0
    dy = (side - (y1 - y0)) / 2 - y0
    s = round(side, 2)
    (BRAND / "mark.svg").write_text(svg(s, s, rects(icon.MARK, {"text": INK, "sound": ULTRA, "cursor": ULTRA}, dx, dy)))
    (BRAND / "mark-mono.svg").write_text(svg(s, s, rects(icon.MARK, {"text": "currentColor", "sound": "currentColor", "cursor": "currentColor"}, dx, dy)))
    (BRAND / "mark-plate.svg").write_text(svg(64, 64, plate(64)))
    (BRAND / "favicon.svg").write_text(svg(32, 32, plate(32, small=True)))
    (BRAND / "wordmark.svg").write_text(wordmark(INK))
    (BRAND / "wordmark-dark.svg").write_text(wordmark(PAPER))


def ttf(name):
    from fontTools.ttLib import TTFont
    out = Path("/tmp") / (name + ".ttf")
    if not out.exists():
        f = TTFont(ROOT / "fonts" / f"{name}.woff2")
        f.flavor = None
        f.save(out)
    return str(out)


def social():
    """1280×640, the GitHub social card. Paper ground like the site; the
    signature band runs along the bottom: sound on the right resolving into
    lines of text on the left."""
    from PIL import Image, ImageDraw, ImageFont
    W, H, SS = 1280, 640, 2
    img = Image.new("RGB", (W * SS, H * SS), PAPER)
    d = ImageDraw.Draw(img)
    hexrgb = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    # The resolve band.
    import math
    y_mid, left, right = 548 * SS, 96 * SS, (W - 96) * SS
    n = 64
    step = (right - left) / n
    for i in range(n):
        t = i / (n - 1)
        x = left + i * step
        if t < .5:   # text: flat lines of varying length, stacked like a paragraph
            continue
        v = abs(math.sin(i * .61) * .6 + math.sin(i * .23 + 1) * .4)
        h = (6 + v * 54 * (t - .5) * 2) * SS
        d.rounded_rectangle((x, y_mid - h / 2, x + step * .5, y_mid + h / 2),
                            radius=step * .25, fill=hexrgb(ULTRA) if i % 7 == 3 else (196, 205, 222))
    lens = (.98, .84, .93, .61)
    for j, ln in enumerate(lens):
        y = y_mid - 27 * SS + j * 16 * SS
        x1 = left + (right - left) * .5 * ln - step * .5
        d.rounded_rectangle((left, y, x1, y + 6 * SS), radius=1.5 * SS,
                            fill=hexrgb(INK) if j == 0 else (196, 205, 222))
    # the find: one marker on the second line
    y = y_mid - 27 * SS + 16 * SS
    d.rounded_rectangle((left + 180 * SS, y - 5 * SS, left + 330 * SS, y + 11 * SS), radius=2 * SS, fill=hexrgb(MARKER))
    d.rounded_rectangle((left + 186 * SS, y, left + 324 * SS, y + 6 * SS), radius=1.5 * SS, fill=hexrgb(INK))

    ic = icon.render(208).resize((208 * SS, 208 * SS), Image.LANCZOS)
    img.paste(ic, (78 * SS, 92 * SS), ic)

    g7 = ImageFont.truetype(ttf("space-grotesk-latin-700-normal"), 132 * SS)
    g4 = ImageFont.truetype(ttf("space-grotesk-latin-400-normal"), 38 * SS)
    mono = ImageFont.truetype(ttf("space-mono-latin-400-normal"), 21 * SS)
    d.text((322 * SS, 214 * SS), "Fundus", font=g7, fill=hexrgb(INK), anchor="ls")
    d.text((326 * SS, 286 * SS), "Podcasts and videos, as text", font=g4, fill=hexrgb(INK), anchor="ls")
    d.text((326 * SS, 334 * SS), "your AI can work with.", font=g4, fill=(89, 97, 111), anchor="ls")
    tags = "TRANSCRIPTS  ·  PODCAST PACKAGES  ·  MCP"
    d.text((326 * SS, 404 * SS), tags, font=mono, fill=hexrgb(ULTRA), anchor="ls")
    img = img.resize((W, H), Image.LANCZOS)
    img.save(ROOT / "docs" / "social-preview.png", optimize=True)


if __name__ == "__main__":
    write_svgs()
    if "--no-png" not in sys.argv:
        social()
    print("→ assets/brand/*.svg, docs/social-preview.png")
