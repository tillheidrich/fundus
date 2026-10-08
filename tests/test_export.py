"""Filename templates and the error log.

The filename comes from user input and ends up in a Content-Disposition
header, so the sanitising here is a security boundary, not cosmetics.
"""
import pytest

import main


META = {"title": "Mein Video: Teil 1", "uploader": "Kanal",
        "upload_date": "20260812", "webpage_url": "https://youtu.be/abc123XYZ_-"}
URL = "https://www.youtube.com/watch?v=abc123XYZ_-"


# ── Placeholders ──────────────────────────────────────────────────────────────

def test_title_placeholder():
    assert main._render_filename("[title]", META, URL) == "Mein Video: Teil 1"


def test_video_id_placeholder():
    assert main._render_filename("[id]", META, URL) == "abc123XYZ_-"


@pytest.mark.parametrize("url", [
    "https://youtu.be/abc123XYZ_-",
    "https://www.youtube.com/watch?v=abc123XYZ_-",
    "https://www.youtube.com/shorts/abc123XYZ_-",
])
def test_video_id_from_every_url_shape(url):
    assert main._render_filename("[id]", META, url) == "abc123XYZ_-"


def test_combined_placeholders():
    out = main._render_filename("[uploader] - [title] ([publishDate])", META, URL)
    assert out == "Kanal - Mein Video: Teil 1 (12.08.2026)"


def test_placeholders_are_case_insensitive():
    assert main._render_filename("[TITLE]", META, URL) == main._render_filename("[title]", META, URL)


def test_current_date_placeholder():
    from datetime import datetime
    assert main._render_filename("[currentDate]", META, URL) == datetime.now().strftime("%Y-%m-%d")


def test_unknown_placeholder_is_left_alone():
    assert "[nope]" in main._render_filename("x[nope]", META, URL)


def test_empty_template_falls_back():
    assert main._render_filename("", META, URL) == "Mein Video: Teil 1"


# ── Sanitising (security boundary) ────────────────────────────────────────────

@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "..\\..\\windows",
    'name"; rm -rf /',
    "line\r\nInjected-Header: x",
    "with\x00null",
])
def test_dangerous_filenames_are_neutralised(evil):
    out = main._safe_filename(evil)
    for bad in ("/", "\\", '"', "\r", "\n", "\x00"):
        assert bad not in out


def test_filename_length_is_capped():
    assert len(main._safe_filename("a" * 500)) <= 120


def test_empty_filename_gets_a_default():
    assert main._safe_filename("   ...  ") == "transkript"


def test_template_with_path_traversal_is_sanitised():
    meta = {**META, "title": "../../secret"}
    out = main._render_filename("[title]", meta, URL)
    assert "/" not in out


# ── Error log ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_log():
    main._error_log.clear()
    yield
    main._error_log.clear()


def test_log_records_entry():
    main.log_error("Download", "boom", "https://example.com/x", "till")
    e = main._error_log[0]
    assert e["where"] == "Download" and e["message"] == "boom"
    assert e["url"] == "https://example.com/x" and e["user"] == "till"
    assert e["at"]


def test_newest_entry_comes_first():
    main.log_error("A", "first")
    main.log_error("B", "second")
    assert main._error_log[0]["where"] == "B"


def test_log_is_bounded(monkeypatch):
    """A retry loop must not be able to eat memory."""
    from collections import deque
    monkeypatch.setattr(main, "_error_log", deque(maxlen=5))
    for i in range(50):
        main.log_error("X", f"msg {i}")
    assert len(main._error_log) == 5
    assert main._error_log[0]["message"] == "msg 49"


def test_long_messages_are_truncated():
    main.log_error("X", "y" * 5000)
    assert len(main._error_log[0]["message"]) <= 600


def test_whitespace_is_collapsed():
    main.log_error("X", "line1\n\n   line2\t\tend")
    assert main._error_log[0]["message"] == "line1 line2 end"
