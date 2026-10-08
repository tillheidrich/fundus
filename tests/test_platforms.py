"""Platform detection and the Threads extractor's field parsing.

Threads is read from the JSON Meta embeds in the page — there is no API
contract, so these tests pin down the shape we rely on.
"""
import pytest

import main


# ── URL routing ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/reel/ABC/", "Instagram"),
    ("https://www.tiktok.com/@x/video/1", "TikTok"),
    ("https://www.threads.net/@x/post/ABC", "Threads"),
    ("https://www.threads.com/@x/post/ABC", "Threads"),
    ("https://youtu.be/abc", "YouTube"),
    ("https://www.youtube.com/watch?v=abc", "YouTube"),
    ("https://example.com/video", ""),
])
def test_platform_detection(url, expected):
    assert main._platform_of(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.threads.net/@x/post/ABC",
    "https://www.threads.com/@x/post/ABC",
])
def test_threads_detection(url):
    assert main._is_threads(url)


def test_threads_detection_negative():
    assert not main._is_threads("https://www.instagram.com/p/ABC/")


@pytest.mark.parametrize("url", [
    "https://youtu.be/abc",
    "https://www.youtube.com/watch?v=abc",
    "https://m.youtube.com/watch?v=abc",
])
def test_youtube_detection(url):
    assert main._is_youtube(url)


# ── Threads post normalisation ────────────────────────────────────────────────

def test_threads_text_only_post():
    """The common Threads case: no media at all, text is the whole content."""
    item = {"post": {
        "code": "ABC123",
        "taken_at": 1_700_000_000,
        "user": {"username": "creator"},
        "caption": {"text": "Ein reiner Textbeitrag."},
    }}
    p = main._threads_post_fields(item)
    assert p["text"] == "Ein reiner Textbeitrag."
    assert p["user"] == "creator"
    assert p["images"] == [] and p["videos"] == []


def test_threads_falls_back_to_text_fragments():
    item = {"post": {
        "code": "X",
        "user": {"username": "u"},
        "caption": None,
        "text_post_app_info": {"text_fragments": {"fragments": [
            {"plaintext": "Teil eins "}, {"plaintext": "und zwei"},
        ]}},
    }}
    assert main._threads_post_fields(item)["text"] == "Teil eins und zwei"


def test_threads_picks_largest_image():
    item = {"post": {
        "code": "X", "user": {"username": "u"}, "caption": {"text": "t"},
        "image_versions2": {"candidates": [
            {"url": "small.jpg", "width": 320},
            {"url": "large.jpg", "width": 1080},
        ]},
    }}
    assert main._threads_post_fields(item)["images"] == ["large.jpg"]


def test_threads_video_wins_over_thumbnail():
    item = {"post": {
        "code": "X", "user": {"username": "u"}, "caption": {"text": "t"},
        "video_versions": [{"url": "clip.mp4"}],
        "image_versions2": {"candidates": [{"url": "thumb.jpg", "width": 640}]},
    }}
    p = main._threads_post_fields(item)
    assert p["videos"] == ["clip.mp4"]
    assert p["images"] == []          # thumbnail must not be downloaded as well


def test_threads_carousel_collects_all_children():
    item = {"post": {
        "code": "X", "user": {"username": "u"}, "caption": {"text": "t"},
        "carousel_media": [
            {"image_versions2": {"candidates": [{"url": "a.jpg", "width": 1080}]}},
            {"image_versions2": {"candidates": [{"url": "b.jpg", "width": 1080}]}},
            {"video_versions": [{"url": "c.mp4"}]},
        ],
    }}
    p = main._threads_post_fields(item)
    assert p["images"] == ["a.jpg", "b.jpg"]
    assert p["videos"] == ["c.mp4"]


def test_threads_rejects_non_post_items():
    assert main._threads_post_fields({"not_a_post": 1}) is None


def test_threads_walk_finds_nested_chains():
    payload = {"a": {"b": [{"thread_items": [{"post": {"code": "1"}}]}]},
               "c": {"thread_items": [{"post": {"code": "2"}}]}}
    found: list = []
    main._threads_walk(payload, found)
    assert len(found) == 2


# ── Format selection ──────────────────────────────────────────────────────────

def test_format_selector_has_best_fallback_for_instagram():
    """Instagram serves progressive formats without a height label — without the
    trailing /best the height filter alone fails."""
    sel = main._format_selector("720")
    assert sel.endswith("/best")
    assert "height<=720" in sel


def test_format_selector_default():
    assert main._format_selector("best") == "bestvideo+bestaudio/best"
