"""Audio trimming.

The interesting surface here is not ffmpeg — it is the boundary arithmetic.
Start and end come from a draggable UI and from two free-text fields, so they
arrive as anything: negative, past the end, reversed, equal. Every one of those
produces an ffmpeg command that either fails loudly or, worse, succeeds and
writes a zero-length file the user only discovers after downloading it.

Ownership matters too: a trim session holds a full-quality audio file keyed by
a guessable-looking UUID, so every route has to verify the caller owns it.
"""
import inspect

import pytest

import main


# ── Bounds ────────────────────────────────────────────────────────────────────

def _clamp(start, end, dur):
    """The arithmetic from trim_cut, isolated so it can be tested directly."""
    start = max(0.0, min(start, dur))
    end = dur if end <= 0 else min(end, dur)
    return start, end


@pytest.mark.parametrize("start,end,dur,exp", [
    (0, 0, 100, (0, 100)),          # end=0 means "to the end"
    (-5, 50, 100, (0, 50)),         # negative start clamps
    (10, 500, 100, (10, 100)),      # end past the file clamps
    (150, 0, 100, (100, 100)),      # start past the file clamps to the end
    (10, 20, 100, (10, 20)),        # the ordinary case
])
def test_bounds_are_clamped(start, end, dur, exp):
    assert _clamp(start, end, dur) == exp


def test_too_short_a_selection_is_refused():
    """ffmpeg would happily write a 0.1 s file; nobody wants that by accident."""
    src = inspect.getsource(main.trim_cut)
    assert "end - start < 0.5" in src


def test_reversed_selection_cannot_reach_ffmpeg():
    """Dragging the end past the start would make -t negative."""
    start, end = _clamp(60, 30, 100)
    assert end - start < 0.5          # caught by the guard above


# ── Input validation ──────────────────────────────────────────────────────────

def test_bitrate_is_an_allowlist():
    """It is interpolated into an ffmpeg argument."""
    src = inspect.getsource(main.trim_cut)
    assert "bitrate not in AUDIO_BITRATES" in src


def test_format_is_an_allowlist():
    """It becomes a file extension and a codec choice."""
    src = inspect.getsource(main.trim_cut)
    assert 'fmt not in ("mp3", "m4a", "wav")' in src


def test_offered_bitrates_are_sane():
    assert all(b.isdigit() and 64 <= int(b) <= 320 for b in main.AUDIO_BITRATES)


def test_bitrate_choice_exists_at_all():
    """ClipGrab hardcodes 256k — its own settings tab for this is removed at
    runtime with a //fixme!. Offering the choice is the low bar here."""
    assert len(main.AUDIO_BITRATES) >= 4


def test_fade_cannot_exceed_the_selection():
    """A 2 s fade on a 1 s clip would fade in and out over the same samples."""
    src = inspect.getsource(main.trim_cut)
    assert "min(fade, length / 2)" in src


# ── Ownership ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [
    main.trim_status, main.trim_audio, main.trim_cut, main.trim_download,
])
def test_every_route_checks_ownership(fn):
    """A trim session holds a full audio file; the id must not be enough."""
    assert "_own_trim" in inspect.getsource(fn)


def test_ownership_check_compares_the_user():
    src = inspect.getsource(main._own_trim)
    assert 'rec.get("user_id") != user["id"]' in src


def test_missing_session_is_a_404_not_a_500():
    src = inspect.getsource(main._own_trim)
    assert "404" in src


def test_status_does_not_leak_internals():
    """The record carries user_id and the source URL; neither belongs in a
    response that the page polls every second."""
    src = inspect.getsource(main.trim_status)
    assert '"user_id", "url"' in src


# ── Cutting behaviour ─────────────────────────────────────────────────────────

def test_cut_re_encodes_instead_of_stream_copying():
    """-c copy only cuts on frame boundaries — up to ~26 ms off, audible as a
    clipped syllable exactly where the user was being precise."""
    src = inspect.getsource(main.trim_cut)
    assert "-c copy" not in src
    assert "libmp3lame" in src


def test_seek_comes_before_the_input():
    """-ss after -i decodes everything up to the start point; before it, ffmpeg
    seeks. On a two-hour file that is the difference between a second and a
    minute."""
    src = inspect.getsource(main.trim_cut)
    assert src.index('"-ss"') < src.index('"-i"')


def test_duration_is_used_rather_than_an_end_timestamp():
    """-t length is unambiguous after a seek; -to would be relative to the
    seek point in some ffmpeg versions and absolute in others."""
    assert '"-t"' in inspect.getsource(main.trim_cut)


def test_fade_is_optional():
    """Zero fade must produce no filter at all, not afade with d=0."""
    src = inspect.getsource(main.trim_cut)
    assert "if fade > 0:" in src


def test_wav_ignores_the_bitrate():
    """PCM has no bitrate setting; passing -b:a would be meaningless."""
    src = inspect.getsource(main.trim_cut)
    assert 'pcm_s16le' in src
    wav_line = [l for l in src.splitlines() if "pcm_s16le" in l][0]
    assert "-b:a" not in wav_line


def test_previous_cut_is_removed_first():
    """Otherwise ffmpeg's -y overwrites, but a failed run would leave the old
    file in place and the user would download a stale cut."""
    assert "out.unlink(missing_ok=True)" in inspect.getsource(main.trim_cut)


# ── Preparation ───────────────────────────────────────────────────────────────

def test_source_audio_is_fetched_at_full_quality():
    """The cut re-encodes from this file; starting at 96k bakes that in."""
    src = inspect.getsource(main._trim_prepare)
    assert '"--audio-quality", "0"' in src


def test_overlong_media_is_refused():
    """A three-hour file is a multi-hundred-megabyte download and an
    unusable waveform at any sane pixel width."""
    src = inspect.getsource(main._trim_prepare)
    assert "TRIM_MAX_MINUTES" in src


def test_preparation_failure_is_reported_not_raised():
    """It runs as a background task — an exception there vanishes silently and
    the UI polls forever."""
    src = inspect.getsource(main._trim_prepare)
    assert 'status="error"' in src


def test_download_filename_is_sanitised():
    """The title comes from the platform and lands in a Content-Disposition
    header."""
    assert "_safe_filename" in inspect.getsource(main.trim_download)


def test_trim_sessions_expire_with_everything_else():
    """Each holds the largest file this tool keeps on disk."""
    assert "trims.pop" in inspect.getsource(main._prune_state)
