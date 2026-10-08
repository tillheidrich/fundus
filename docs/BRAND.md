# Fundus brand

## The idea

*Fundus* is a stock you draw from: the theatre's prop store, a find
(*Fund*), the Latin *fundus*, ground. Fundus the tool takes what was
only ever said and turns it into material you can come back to and quote.

So the brand is about one moment, **sound settling into text**, and one
reward, **the find**: the line you were looking for, highlighted.

## The mark

A capital F set like a paragraph, plus a cursor.

- **The stem** is the left margin of a page.
- **The middle arm** is a line of text.
- **The top arm** starts as text at the stem and, further right, is
  still sound: three rounded bars of different heights.
- **The cursor** (the underscore that already follows the name in the app,
  `Fundus_`) sits on the baseline. It is the ground the letter stands on
  and the place where the find appears.

One rule carries the whole drawing: **sound is round, text is square.**
Bars have fully rounded ends; text strokes have a small, fixed corner.

Files, all generated (see "Source of truth" below):

| File | Use |
|---|---|
| `assets/brand/mark.svg` | Mark on light grounds: ink F, ultramarine sound and cursor |
| `assets/brand/mark-mono.svg` | One colour, `currentColor`; for embossing, single-ink print, UI glyphs |
| `assets/brand/mark-plate.svg` | Mark on the ultramarine plate, 64 px artboard |
| `assets/brand/favicon.svg` | Plate, small cut (two bars, heavier strokes) for 16 to 32 px |
| `assets/brand/wordmark.svg` / `wordmark-dark.svg` | Plate + name, outlined Space Grotesk 700 |
| `macos/AppIcon.icns`, `site/icon.png` | App icon: squircle body per Apple's 824/1024 template, lit plate, lifted glyph |
| `docs/social-preview.png` | 1280×640 GitHub social card |

Below 48 px every rendering switches to the small cut. Three bars at 16 px
merge into a smear; two wider bars still read as "something arriving".

Clear space around the mark: the width of the stem on every side. Minimum
size: 16 px for the plate, 20 px for the bare mark.

## Colour

Strategy: restrained. Tinted neutrals carry the surface; ultramarine is the
one colour you can act on; the marker appears only where something was found.

| Role | Token | Hex | OKLCH |
|---|---|---|---|
| Ink (text, primary button) | `--ink` | `#111826` | `oklch(0.209 0.030 264)` |
| Paper (page) | `--paper` | `#F4F7FB` | `oklch(0.975 0.006 255)` |
| Card | `--card` | `#FCFDFE` | `oklch(0.994 0.002 248)` |
| Muted text | `--muted` | `#59616F` | `oklch(0.491 0.025 262)` |
| Ultramarine (action, focus, the plate) | `--blue` | `#203AD2` | `oklch(0.450 0.231 267)` |
| Ultramarine deep (hover, small links) | `--blue-dark` | `#1627AF` | `oklch(0.380 0.209 267)` |
| Ultramarine soft (tint) | `--blue-soft` | `#E9F1FF` | `oklch(0.956 0.021 262)` |
| Marker (the find) | `--marker` | `#DEF766` | `oklch(0.930 0.171 118)` |

Dark grounds (the privacy section of the site, code panels, the social card
on dark, GitHub dark mode):

| Role | Hex | OKLCH |
|---|---|---|
| Ground | `#111826` | `oklch(0.209 0.030 264)` |
| Raised | `#1A2232` | `oklch(0.252 0.033 264)` |
| Text | `#E6EBF3` | `oklch(0.939 0.012 260)` |
| Secondary text | `#A7B0C0` | `oklch(0.755 0.025 262)` |
| Ultramarine on dark | `#A4B4FF` | `oklch(0.786 0.109 274)` |
| Marker | `#DEF766` | unchanged |

Contrast pairs (WCAG 2.x, all AA for normal text unless marked):

| Foreground on background | Ratio |
|---|---|
| Ink on paper / card | 16.5 / 17.4 |
| Muted on paper / card | 5.8 / 6.1 |
| Card on ultramarine (button text, plate glyph) | 7.9 |
| Ultramarine deep on paper | 10.1 |
| Ultramarine deep on ultramarine soft | 9.5 |
| Ink on marker | 14.9 |
| Ultramarine on marker | 6.8 |
| Text / secondary on dark ground | 14.8 / 8.1 |
| Ultramarine on dark, on dark ground | 8.9 |
| Marker on dark ground | 14.9 |
| `--line-strong` on card (control edges, non-text, 3:1 needed) | 4.0 |

The marker on paper is 1.1:1. That is why the rules below exist.

**Why ultramarine stays.** The app's token set was tuned and is pinned by
`tests/test_design_tokens.py`; the hue is saturated enough to own, and it
is not the default "tool blue" (it sits at hue 267, close to pigment
ultramarine, not at the 250 of most UI kits). What was missing was a second
voice, and the marker is it.

## Typography

- **Space Grotesk** (400, 500, 700) for everything you read. No 600 exists;
  asking for it renders 700.
- **Space Mono** (400, 700) for timestamps, file names, labels in caps,
  and anything a machine produced.
- Both are self-hosted from `fonts/` (OFL). No CDN, ever: the site's CSP
  forbids it and the privacy promise depends on it.
- The wordmark is Space Grotesk 700 at -2 % tracking, outlined to paths.
- Product UI: ratio about 1.2 between steps. Marketing: display sizes at
  -3.5 % tracking, weight 500, never bold headlines.

## Iconography

- UI icons are line icons, 1.5 px at 16 to 20 px, round caps and joins,
  drawn on the same 4 px grid as the mark. `currentColor` only.
- Sound is round, text is square, here as well: anything that stands for
  audio may have round ends; files and text get square shapes.
- No platform logos anywhere in the product or on the site. Name a
  platform in text (YouTube, Spotify), never with its mark.
- No emoji as icons.

## Voice

1. Say what it does, then stop. "Paste a link, get a transcript."
2. Concrete over clever: numbers, file names and timestamps beat adjectives.
3. Honest about limits; say what Fundus does not do before anyone asks.
4. First person singular where it is personal ("I built this for my podcast research"), never a corporate "we".
5. No hype words, no exclamation marks, no em dashes.

## Do

- Use the plate mark wherever the app is represented as an object
  (Dock, favicon, header, README).
- Use the marker for exactly one thing per view: the found line, the
  search hit, the selected file.
- Keep the plate one hue: lightness may change from top to bottom, the hue
  may not.
- Put ink text on the marker, never the other way round on light grounds.

## Don't

- Don't set text in the marker colour on paper or card (1.1:1).
- Don't use the marker for buttons, links or states; it is not interactive.
- Don't recolour the plate, add a second hue to it, or outline it.
- Don't stretch, rotate, or rearrange the bars; their heights are the drawing.
- Don't redraw the mark from the SVGs by hand. Change the geometry in
  `macos/make-icon.py` and regenerate.
- No stock gradients, glows, glass or 3D extrusions on the mark.

## Source of truth

`macos/make-icon.py` holds the geometry (`MARK`, `MARK_SMALL`) and builds
the app icon and `site/icon.png`. `tools/brand.py` imports that geometry
and writes the SVGs and the social card. Regenerate with:

    python3 -m venv /tmp/iconenv
    /tmp/iconenv/bin/pip install Pillow fonttools brotli
    /tmp/iconenv/bin/python macos/make-icon.py
    /tmp/iconenv/bin/python tools/brand.py
    python3 site/build.py

The inline copies of the mark in `templates/*.html` and `site/build.py`
are rounded from `mark-plate.svg` and `favicon.svg`; update them when the
geometry changes.
