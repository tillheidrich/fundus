#!/usr/bin/env python3
"""Build macos/AppIcon.icns and site/icon.png from code.

Why a generator and not a committed .icns: a binary in the repository is a
thing nobody can review and nobody can change without the original design
file. This script *is* the design file for the mark. tools/brand.py imports
the geometry from here, so the SVGs, the favicon and the app icon cannot
drift apart. It needs Pillow, which is not a runtime dependency of Fundus;
install it into a throwaway environment:

    python3 -m venv /tmp/iconenv && /tmp/iconenv/bin/pip install Pillow
    /tmp/iconenv/bin/python macos/make-icon.py            # icns + site/icon.png
    /tmp/iconenv/bin/python macos/make-icon.py --preview  # PNGs to /tmp

The mark (see docs/BRAND.md): a capital F set like a paragraph. The stem is
the left margin, the middle arm is a line of text, the top arm comes in as
sound and only turns into a line where it meets the stem. Next to it sits
the cursor, the underscore that already follows the name in the app. Sound
is drawn round, text is drawn square: that is the whole idea, at any size.

Drawn at four times the target size and reduced, because Pillow does not
antialias shape edges; without the downscale every curve is a staircase.
"""

from pathlib import Path
import math
import subprocess
import sys

# ── Geometry, on a 48-unit grid. Shared with tools/brand.py. ────────────────
# (x, y, w, h, radius, role). Roles: "text" (the F), "sound" (the bars),
# "cursor" (the underscore). Radii: text gets a small, decided corner; sound
# is fully round.
TEXT_R = 1.4
MARK = [
    (10.0, 9.0, 7.0, 30.0, TEXT_R, "text"),     # stem: the left margin
    (10.0, 9.0, 15.0, 7.0, TEXT_R, "text"),     # top arm, the part already text
    (10.0, 21.0, 13.0, 6.0, TEXT_R, "text"),    # middle arm: one line of text
    (27.0, 6.5, 3.4, 12.0, 1.7, "sound"),       # sound, arriving from the right
    (32.4, 3.5, 3.4, 18.0, 1.7, "sound"),
    (37.8, 8.0, 3.4, 9.0, 1.7, "sound"),
    (21.0, 34.0, 12.0, 5.0, TEXT_R, "cursor"),  # the cursor: ground and find
]
# Below 48 px the three bars merge into a smear. The small cut keeps two,
# wider, so the F still reads as "F with something arriving".
MARK_SMALL = [
    (9.0, 8.0, 8.0, 32.0, 1.6, "text"),
    (9.0, 8.0, 17.0, 8.0, 1.6, "text"),
    (9.0, 21.0, 14.0, 7.0, 1.6, "text"),
    (29.0, 5.0, 5.0, 14.0, 2.5, "sound"),
    (36.5, 8.0, 5.0, 8.0, 2.5, "sound"),
    (21.5, 33.0, 14.0, 7.0, 1.6, "cursor"),
]


def bbox(shapes):
    xs0 = min(s[0] for s in shapes); ys0 = min(s[1] for s in shapes)
    xs1 = max(s[0] + s[2] for s in shapes); ys1 = max(s[1] + s[3] for s in shapes)
    return xs0, ys0, xs1, ys1


# ── Palette (docs/BRAND.md). sRGB values of the OKLCH definitions. ──────────
ULTRA = (32, 58, 210)        # #203AD2  oklch(0.450 0.231 267)
ULTRA_TOP = (52, 82, 228)    # lit edge of the plate, same hue
ULTRA_DEEP = (22, 39, 175)   # #1627AF  oklch(0.380 0.209 267)
PAPER = (244, 247, 251)      # #F4F7FB
MARKER = (222, 247, 102)     # #DEF766  oklch(0.930 0.170 118)
INK = (17, 24, 38)           # #111826

S = 1024
SS = 4
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # importable without Pillow, for tools/brand.py's SVG part
    Image = None


def squircle(n: int, box: tuple, exp: float = 4.6, steps: int = 1440):
    """Superellipse |x|^e + |y|^e = 1. With e≈5 its corners follow the
    continuous curvature of Apple's icon template closely enough that the
    icon does not look like a foreign object in the Dock, which a plain
    rounded rectangle does."""
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + rx * math.copysign(abs(c) ** (2 / exp), c),
                    cy + ry * math.copysign(abs(s) ** (2 / exp), s)))
    return pts


def draw_mark(d, shapes, ox, oy, k, colours):
    for x, y, w, h, r, role in shapes:
        d.rounded_rectangle((ox + x * k, oy + y * k, ox + (x + w) * k, oy + (y + h) * k),
                            radius=r * k, fill=colours[role])


def render(size: int) -> "Image.Image":
    n = size * SS
    small = size <= 32
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # Apple's template: an 824 body in a 1024 canvas. Small sizes get a
    # slightly larger body; at 16 px every pixel of margin is lost detail.
    inset = (0.075 if small else 100 / 1024) * n
    body = (inset, inset, n - inset, n - inset)
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).polygon(squircle(n, body), fill=255)

    if not small:
        # Drop shadow, as in the template: short and soft, below the body.
        sh = Image.new("L", (n, n), 0)
        sh.paste(mask, (0, int(n * 0.012)))
        sh = sh.filter(ImageFilter.GaussianBlur(n * 0.014))
        img.paste((10, 14, 40, 0), (0, 0), sh.point(lambda v: v * 0.32))

    # The plate: one hue, lit from above. Lightness moves, the hue does not —
    # a gradient into a second colour is the stock look this avoids.
    grad = Image.new("RGBA", (1, 256))
    for i in range(256):
        t = i / 255
        c = tuple(round(a + (b - a) * t / .55) for a, b in zip(ULTRA_TOP, ULTRA)) if t < .55 else \
            tuple(round(a + (b - a) * (t - .55) / .45) for a, b in zip(ULTRA, ULTRA_DEEP))
        grad.putpixel((0, i), c + (255,))
    grad = grad.resize((n, n), Image.BILINEAR)
    img.paste(grad, (0, 0), mask)

    if not small:
        # A hairline of light along the top edge, inside the body.
        rim = Image.new("L", (n, n), 0)
        ImageDraw.Draw(rim).polygon(squircle(n, body), fill=255)
        inner = Image.new("L", (n, n), 0)
        off = n * 0.006
        ImageDraw.Draw(inner).polygon(
            squircle(n, (body[0], body[1] + off, body[2], body[3] + off)), fill=255)
        rim = Image.composite(Image.new("L", (n, n), 0), rim, inner)
        fade = Image.linear_gradient("L").resize((n, n)).point(lambda v: max(0, 255 - v * 3))
        rim = Image.composite(rim, Image.new("L", (n, n), 0), fade)
        img.paste((255, 255, 255, 255), (0, 0), rim.point(lambda v: v * 0.22))

    shapes = MARK_SMALL if small else MARK
    x0, y0, x1, y1 = bbox(shapes)
    span = (body[2] - body[0]) * (0.62 if small else 0.54)
    k = span / max(x1 - x0, y1 - y0)
    ox = n / 2 - (x0 + x1) / 2 * k
    # The bars reach above the F, so the bounding box sits higher than the
    # glyph's visual weight. Nudge down to the optical centre.
    oy = n / 2 - (y0 + y1) / 2 * k + n * (0.01 if small else 0.018)
    colours = {"text": PAPER, "sound": PAPER, "cursor": MARKER}

    if not small:
        # The glyph sits a little above the plate: a soft, ink-tinted shadow.
        gs = Image.new("L", (n, n), 0)
        draw_mark(ImageDraw.Draw(gs), shapes, ox, oy + n * 0.010, k,
                  {"text": 255, "sound": 255, "cursor": 255})
        gs = gs.filter(ImageFilter.GaussianBlur(n * 0.012))
        img.paste((8, 14, 70, 255), (0, 0), gs.point(lambda v: v * 0.38))

    draw_mark(ImageDraw.Draw(img), shapes, ox, oy, k, colours)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    if Image is None:
        sys.exit("Pillow fehlt. Siehe Kommentar oben im Skript.")
    if "--preview" in sys.argv:
        for size in (16, 32, 128, 1024):
            render(size).save(f"/tmp/fundus-icon-{size}.png")
        print("→ /tmp/fundus-icon-{16,32,128,1024}.png")
        return

    iconset = HERE / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    # The list iconutil expects. A missing size makes macOS scale the next
    # larger one, visibly soft in the Dock.
    for size in (16, 32, 64, 128, 256, 512, 1024):
        img = render(size)
        if size <= 512:
            img.save(iconset / f"icon_{size}x{size}.png")
        if size >= 32:
            img.save(iconset / f"icon_{size // 2}x{size // 2}@2x.png")

    out = HERE / "AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    for f in iconset.iterdir():
        f.unlink()
    iconset.rmdir()
    render(256).save(ROOT / "site" / "icon.png", optimize=True)
    print(f"→ {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB), site/icon.png")


if __name__ == "__main__":
    main()
