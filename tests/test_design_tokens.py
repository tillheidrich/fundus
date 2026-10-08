"""Design tokens and contrast.

The colour migration was done largely with sed across two template files. That
is the right tool for ~200 mechanical replacements and the wrong one for
catching what it missed — a `var(--teal)` left behind renders as *nothing*,
silently, and only on the one screen you did not open. These tests are the
safety net for that class of mistake.

They also pin the accessibility floor. The border the migration replaced sat
at 1.39:1 against the page, which fails WCAG 1.4.11 for anything that
identifies a control. It had been that way for months without anyone noticing,
which is exactly why it belongs in a test rather than in a review checklist.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
INDEX = (TEMPLATES / "index.html").read_text()
LOGIN = (TEMPLATES / "login.html").read_text()

# yt-dlp command-line flags live in the generated shell scripts and look just
# like CSS custom properties to a regex.
CLI_FLAGS = {
    "--no-check-certificates", "--cookies-from-browser", "--audio-format",
    "--download-sections", "--live-from-start", "--quiet", "--output",
    "--model", "--language", "--pre", "--sub-langs", "--sub-format",
    "--skip-download", "--no-playlist", "--extractor-args", "--write-auto-sub",
    "--write-sub", "--audio-quality", "--cookies", "--simulate",
    "--list-subs", "--no-warnings", "--print",
}


def _defined(css: str) -> set[str]:
    root = re.search(r":root\{(.*?)\n    \}", css, re.S)
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", root.group(1))) if root else set()


def _used(css: str) -> set[str]:
    return set(re.findall(r"var\((--[a-z0-9-]+)", css)) - CLI_FLAGS


# ── Nothing left behind, nothing undefined ────────────────────────────────────

@pytest.mark.parametrize("name,css", [("index", INDEX), ("login", LOGIN)])
def test_every_token_used_is_defined(name, css):
    """An undefined custom property renders as nothing at all — no error, no
    fallback, just a missing colour on one screen."""
    missing = _used(css) - _defined(css)
    assert not missing, f"{name}.html benutzt undefinierte Tokens: {sorted(missing)}"


@pytest.mark.parametrize("name,css", [("index", INDEX), ("login", LOGIN)])
def test_no_tokens_from_the_old_palette(name, css):
    """--teal that is blue, --coral that means error: names that lie cost an
    afternoon the next time someone reads this file."""
    for dead in ("--teal", "--coral", "--sunbeam", "--moss", "--warm:"):
        assert dead not in css, f"{name}.html enthält noch {dead}"


@pytest.mark.parametrize("name,css", [("index", INDEX), ("login", LOGIN)])
def test_old_font_families_are_gone(name, css):
    for dead in ("Fraunces", "Public Sans", "IBM Plex Mono"):
        assert dead not in css, f"{name}.html lädt noch {dead}"


def test_both_templates_agree_on_the_shared_tokens():
    """Two token blocks that drift are how a login page ends up looking like a
    different product."""
    shared = _defined(INDEX) & _defined(LOGIN)
    assert len(shared) >= 8
    for tok in shared:
        a = re.search(rf"{tok}:\s*([^;]+);", INDEX)
        b = re.search(rf"{tok}:\s*([^;]+);", LOGIN)
        if a and b:
            assert a.group(1).strip().lower() == b.group(1).strip().lower(), \
                f"{tok} unterscheidet sich zwischen index und login"


# ── Contrast ──────────────────────────────────────────────────────────────────

def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def token(name: str) -> str:
    m = re.search(rf"{name}:\s*(#[0-9A-Fa-f]{{6}})", INDEX)
    assert m, f"{name} nicht als Hex-Wert gefunden"
    return m.group(1)


@pytest.mark.parametrize("fg,bg", [
    ("--ink", "--card"), ("--ink", "--paper"), ("--ink", "--surface-2"),
    ("--muted", "--card"), ("--muted", "--paper"), ("--muted", "--surface-2"),
    ("--blue", "--card"), ("--blue-dark", "--paper"),
    ("--err-ink", "--card"), ("--warn-ink", "--card"),
    ("--label-ink", "--label"), ("--blue-line", "--label"),
])
def test_text_contrast_meets_aa(fg, bg):
    """4.5:1 — this UI puts real information at .62rem, so the large-text
    exemption does not apply anywhere it matters."""
    r = contrast(token(fg), token(bg))
    assert r >= 4.5, f"{fg} auf {bg}: nur {r:.2f}:1"


@pytest.mark.parametrize("bg", ["--blue", "--err"])
def test_white_on_coloured_buttons(bg):
    assert contrast("#FFFFFF", token(bg)) >= 4.5


@pytest.mark.parametrize("bg", ["--card", "--paper", "--surface-2"])
def test_control_borders_meet_non_text_contrast(bg):
    """WCAG 1.4.11: 3:1 for the edge that tells you a field is a field.
    The palette this replaced sat at 1.39:1."""
    r = contrast(token("--line-strong"), token(bg))
    assert r >= 3.0, f"--line-strong auf {bg}: nur {r:.2f}:1"


def test_form_fields_use_the_strong_border():
    """Defining the token is worthless if the inputs still use the faint one."""
    rule = re.search(r"textarea,input\[type=url\][^}]+\}", INDEX, re.S)
    assert rule and "--line-strong" in rule.group(0)


# ── Shape ─────────────────────────────────────────────────────────────────────

def test_radii_are_down_to_three_values():
    """Eleven different radii read as grown-together; three read as decided."""
    found = set(re.findall(r"border-radius:([^;}]+)", INDEX))
    allowed = {"var(--r)", "var(--r-sm)", "999px", "50%"}
    assert found <= allowed, f"Unerwartete Radien: {sorted(found - allowed)}"


def test_card_radius_is_small():
    """14px is the consumer-app signature; 4px is the tool signature."""
    assert re.search(r"--r:\s*4px", INDEX)


def test_no_fractional_borders():
    """1.5px renders blurry at non-integer device pixel ratios."""
    assert "1.5px solid" not in INDEX


def test_click_feedback_scales_instead_of_shifting():
    """translateY on a 1px-bordered element moves the edge and shimmers."""
    assert "active{transform:translateY(1px)}" not in INDEX
    assert "scale(.97)" in INDEX


def test_numbers_that_update_are_tabular():
    """Without this every progress tick shifts the layout sideways."""
    assert "font-variant-numeric:tabular-nums" in INDEX


def test_reduced_motion_is_honoured():
    assert "prefers-reduced-motion" in INDEX


def test_warm_paper_grain_is_gone():
    """Its noise filter was tuned to warm brown and reads as a dirty screen
    on a cool ground."""
    assert "feTurbulence" not in INDEX


def test_shadows_are_cool():
    """Warm-brown shadow on a cool near-white ground reads as smudge."""
    assert "rgba(60,48,30" not in INDEX
