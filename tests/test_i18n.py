"""Translation.

The risky part here is not the lookup — it is `translate_html`, which rewrites
a rendered page with regular expressions. That page contains yt-dlp command
lines, ffmpeg filter graphs and shell scripts that users copy and run. A
substitution that reaches into one of those produces a broken command that
looks plausible, which is worse than an obviously untranslated label.

So the rule is exact matches only, never substrings, and these tests pin it.
"""
import json
import re
from pathlib import Path

import pytest

import i18n

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "locales"


# ── Catalogue integrity ───────────────────────────────────────────────────────

def test_both_languages_ship():
    assert set(i18n.available()) >= {"de", "en"}


def test_catalogues_cover_the_same_keys():
    """A key present in one file and missing in the other is a silent hole:
    the UI falls back to German for that one label only."""
    de = json.loads((LOCALES / "de.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    assert set(de) == set(en), f"Differenz: {set(de) ^ set(en)}"


def test_german_catalogue_is_the_identity():
    """German is the source language; its catalogue exists only so that a
    missing key shows up as missing rather than as a silent fallback."""
    de = json.loads((LOCALES / "de.json").read_text(encoding="utf-8"))
    assert all(k == v for k, v in de.items())


def test_english_actually_translates():
    """Guards against a catalogue that was generated but never filled in."""
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    changed = sum(1 for k, v in en.items() if k != v)
    assert changed > 200, f"nur {changed} Schlüssel tatsächlich übersetzt"


def test_placeholders_survive_translation():
    """${n} in the German string must reappear in the English one, or the
    rendered message loses its number.

    Only value-carrying placeholders are checked. Inline ternaries like
    ${n===1?'r':''} are German adjective inflection and have no English
    counterpart — requiring them to survive would be requiring a bug.
    """
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    value_ph = re.compile(r"\$\{([a-zA-Z_][\w.]*)\}|(%s)|\{([a-z_]+)\}")

    def carried(s: str) -> set[str]:
        return {g for m in value_ph.finditer(s) for g in m.groups() if g}

    for key, val in en.items():
        if key == val:
            continue
        assert carried(key) <= carried(val), \
            f"Platzhalter verloren: {key!r} → {val!r}"


# ── Language resolution ───────────────────────────────────────────────────────

def test_explicit_choice_beats_everything():
    assert i18n.resolve(query_lang="en", cookie_lang="de", accept="de-DE") == "en"


def test_cookie_beats_the_browser():
    """Someone who picked a language must not be overruled on every visit."""
    assert i18n.resolve(cookie_lang="en", accept="de-DE,de;q=0.9") == "en"


def test_browser_header_is_honoured():
    assert i18n.resolve(accept="en-GB,en;q=0.9,de;q=0.4") == "en"


def test_q_values_decide_not_order():
    """'de;q=0.7, en;q=0.9' means English, even though German comes first —
    browsers really do send this."""
    assert i18n.parse_accept_language("de;q=0.7, en;q=0.9") == "en"


def test_regional_variants_collapse_to_the_base():
    assert i18n.normalize("de-AT") == "de"
    assert i18n.normalize("en_US") == "en"


def test_unsupported_language_falls_back():
    assert i18n.normalize("fr") == i18n.DEFAULT_LANG
    assert i18n.parse_accept_language("fr-FR,fr;q=0.9") == i18n.DEFAULT_LANG


def test_garbage_headers_do_not_crash():
    for junk in ("", "   ", ";;;", "q=", "*", "en;q=notanumber"):
        assert i18n.parse_accept_language(junk) in i18n.available()


def test_unknown_key_returns_itself():
    assert i18n.translate("Kein solcher Schlüssel", "en") == "Kein solcher Schlüssel"


# ── translate_html: the part that could break a copied command ───────────────

def test_german_is_passed_through_untouched():
    """The source language needs no work, and doing none is also the fast path."""
    html = "<p>Transkript holen</p>"
    assert i18n.translate_html(html, "de") is html


def test_substrings_are_never_replaced(monkeypatch):
    """'Video' must not rewrite the 'Video' inside 'Video-URL'."""
    monkeypatch.setattr(i18n, "_cache", {"en": {"Video": "Clip"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    out = i18n.translate_html("<label>Video-URL</label>", "en")
    assert "Video-URL" in out and "Clip" not in out


def test_shell_commands_are_not_corrupted(monkeypatch):
    """The page embeds yt-dlp invocations users copy and paste. None of them
    are catalogue keys, so none may change."""
    monkeypatch.setattr(i18n, "_cache", {"en": {"Video": "Clip", "Ton": "Audio"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    cmd = '<code>yt-dlp -f "bestaudio[ext=m4a]" --audio-format mp3 "$URL"</code>'
    assert i18n.translate_html(cmd, "en") == cmd


def test_markup_structure_is_preserved(monkeypatch):
    monkeypatch.setattr(i18n, "_cache", {"en": {"Hallo": "Hello"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    src = '<div class="a"><span>Hallo</span></div>'
    out = i18n.translate_html(src, "en")
    assert out == '<div class="a"><span>Hello</span></div>'


def test_surrounding_whitespace_survives(monkeypatch):
    """Leading and trailing space between tags carries layout in inline flow."""
    monkeypatch.setattr(i18n, "_cache", {"en": {"Hallo": "Hello"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    assert i18n.translate_html("<p>\n  Hallo\n</p>", "en") == "<p>\n  Hello\n</p>"


def test_phrases_containing_markup_are_handled(monkeypatch):
    """'Lädt <b>ohne</b> Login' is several text nodes, not one — it needs the
    whole-phrase pass or it stays German."""
    key = "Lädt <b>ohne</b> Login"
    monkeypatch.setattr(i18n, "_cache", {"en": {key: "Loads <b>without</b> a login"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    out = i18n.translate_html(f"<p>{key}</p>", "en")
    assert "Loads <b>without</b> a login" in out


def test_longer_phrases_win_over_shorter_ones(monkeypatch):
    """Otherwise a short key eats a fragment of a longer one and leaves debris."""
    monkeypatch.setattr(i18n, "_cache", {"en": {
        "Cookies <b>nur</b> privat": "Cookies <b>only</b> for private posts",
        "Cookies <b>nur</b>": "WRONG",
    }})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    out = i18n.translate_html("<p>Cookies <b>nur</b> privat</p>", "en")
    assert "WRONG" not in out


def test_attributes_are_translated(monkeypatch):
    monkeypatch.setattr(i18n, "_cache", {"en": {"Video-URL": "Video URL"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    out = i18n.translate_html('<input placeholder="Video-URL">', "en")
    assert 'placeholder="Video URL"' in out


def test_js_strings_are_translated(monkeypatch):
    monkeypatch.setattr(i18n, "_cache", {"en": {"Abbrechen": "Cancel"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    out = i18n.translate_html("<script>alert('Abbrechen')</script>", "en")
    assert "alert('Cancel')" in out


def test_translation_containing_the_quote_is_skipped(monkeypatch):
    """An apostrophe in a single-quoted literal would terminate it early and
    break the whole script. Leaving the label German is the lesser evil."""
    monkeypatch.setattr(i18n, "_cache", {"en": {"Abbrechen": "Don't"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    out = i18n.translate_html("<script>alert('Abbrechen')</script>", "en")
    assert "alert('Abbrechen')" in out


def test_script_tags_are_never_lost(monkeypatch):
    monkeypatch.setattr(i18n, "_cache", {"en": {"Hallo": "Hello"}})
    monkeypatch.setattr(i18n, "_rendered_cache", {})
    src = "<p>Hallo</p><script>var x=1;</script><p>Hallo</p>"
    out = i18n.translate_html(src, "en")
    assert out.count("<script") == 1 and "var x=1;" in out


def test_number_locale_follows_the_language():
    assert i18n.js_locale("de") == "de-DE"
    assert i18n.js_locale("en") == "en-US"


def test_catalog_json_is_valid_and_embeddable():
    """It is inlined into a <script> block, so it must parse and must not
    carry a closing tag that would end the block early."""
    blob = i18n.catalog_json("en")
    assert json.loads(blob)
    assert "</script" not in blob.lower()
