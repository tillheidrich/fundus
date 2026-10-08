"""Subtitle parsing and export.

These cover the logic with the highest regression risk: turning YouTube's
subtitle formats into clean text. A global word-dedup once removed ~80% of
every transcript (81 instead of 487 words on a real video), so the "real
repetitions survive" test below is the important one.
"""
import json

import pytest

import main


# ── json3 (preferred format: clean, timed, no karaoke overlap) ────────────────

def test_json3_parses_segments_with_timings():
    raw = json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000,
         "segs": [{"utf8": "Hallo "}, {"utf8": "Welt"}]},
        {"tStartMs": 4000, "dDurationMs": 3000, "segs": [{"utf8": "zweiter Satz"}]},
    ]})
    segs = main.json3_to_segments(raw)
    assert [s["text"] for s in segs] == ["Hallo Welt", "zweiter Satz"]
    assert segs[0]["start"] == 0.0 and segs[0]["dur"] == 2.0
    assert segs[1]["start"] == 4.0


def test_json3_skips_whitespace_only_events():
    raw = json.dumps({"events": [
        {"tStartMs": 0, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 1000, "segs": [{"utf8": "echt"}]},
    ]})
    assert [s["text"] for s in main.json3_to_segments(raw)] == ["echt"]


def test_json3_keeps_repeated_phrases():
    """The bug that made transcripts useless: repeated words must survive."""
    raw = json.dumps({"events": [
        {"tStartMs": 0, "segs": [{"utf8": "niemals niemals"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "niemals aufgeben"}]},
    ]})
    text = main.segments_to_text(main.json3_to_segments(raw))
    assert text.count("niemals") == 3


def test_json3_invalid_input_returns_empty():
    assert main.json3_to_segments("not json") == []


# ── VTT fallback (needs sequential overlap removal) ───────────────────────────

VTT_KARAOKE = """WEBVTT
Kind: captions
Language: de

00:00:01.000 --> 00:00:03.000
das ist ein

00:00:03.000 --> 00:00:05.000
das ist ein test von

00:00:05.000 --> 00:00:07.000
test von dem system
"""


def test_vtt_removes_karaoke_overlap():
    assert main.vtt_to_text(VTT_KARAOKE) == "das ist ein test von dem system"


def test_vtt_keeps_genuine_repetitions():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
niemals niemals niemals

00:00:03.000 --> 00:00:05.000
aufgeben und niemals weinen
"""
    assert main.vtt_to_text(vtt).count("niemals") == 4


def test_vtt_strips_headers_and_cue_numbers():
    text = main.vtt_to_text(VTT_KARAOKE)
    for noise in ("WEBVTT", "Kind:", "Language:", "-->"):
        assert noise not in text


def test_vtt_strips_inline_timing_tags():
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
           "hallo<00:00:01.500><c> welt</c>\n")
    assert main.vtt_to_text(vtt) == "hallo welt"


def test_vtt_parses_timestamps():
    segs = main.vtt_to_segments(VTT_KARAOKE)
    assert segs[0]["start"] == 1.0
    assert segs[0]["dur"] == pytest.approx(2.0)


@pytest.mark.parametrize("ts,expected", [
    ("00:00:01.500", 1.5),
    ("00:01:00.000", 60.0),
    ("01:00:00.000", 3600.0),
    ("00:00:02,250", 2.25),          # SRT-style comma
    ("1:02.500", 62.5),              # no hour part
])
def test_timestamp_parsing(ts, expected):
    assert main._ts_to_seconds(ts) == pytest.approx(expected)


# ── Overlap helper ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prev,new,expected", [
    (["a", "b", "c"], ["b", "c", "d"], ["d"]),
    (["a", "b"], ["c", "d"], ["c", "d"]),          # no overlap
    (["a", "b"], ["A", "B", "c"], ["c"]),          # case-insensitive
    ([], ["a"], ["a"]),
])
def test_strip_overlap(prev, new, expected):
    assert main._strip_overlap(prev, new) == expected


# ── Export formats ────────────────────────────────────────────────────────────

SEGMENTS = [
    {"start": 0.0, "dur": 2.0, "text": "erster Satz"},
    {"start": 2.0, "dur": 3.0, "text": "zweiter Satz"},
]


def test_srt_export_is_well_formed():
    srt = main.segments_to_srt(SEGMENTS)
    lines = srt.splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:02,000"
    assert lines[2] == "erster Satz"
    assert "2\n00:00:02,000 --> 00:00:05,000" in srt


def test_vtt_export_has_header_and_dot_separator():
    vtt = main.segments_to_vtt(SEGMENTS)
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.000" in vtt


def test_vtt_export_roundtrips():
    """Exported VTT must parse back to the same text."""
    reparsed = main.vtt_to_text(main.segments_to_vtt(SEGMENTS))
    assert reparsed == main.segments_to_text(SEGMENTS)


def test_timestamped_view():
    out = main.segments_to_timestamped(SEGMENTS)
    assert out.splitlines()[0] == "[0:00] erster Satz"


def test_timestamped_view_uses_hours_when_needed():
    out = main.segments_to_timestamped([{"start": 3661, "dur": 1, "text": "spät"}])
    assert out == "[1:01:01] spät"
