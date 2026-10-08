"""Duplicate detection.

The same post reaches the input in many shapes: pasted twice, once with
Instagram's ?img_index=, once as a youtu.be short link, once with a share
tracking parameter. All of those must collapse to one download.
"""
import pytest

import main


# ── URL normalisation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    # protocol / host / trailing slash noise
    ("https://www.instagram.com/p/ABC/", "http://instagram.com/p/ABC"),
    ("https://m.instagram.com/p/ABC/", "https://www.instagram.com/p/ABC/"),
    # Instagram carousel index — the exact case that produced a double download
    ("https://www.instagram.com/p/ABC/?img_index=1", "https://www.instagram.com/p/ABC/"),
    # share tracking
    ("https://www.instagram.com/p/ABC/?igsh=xyz", "https://www.instagram.com/p/ABC/"),
    ("https://www.instagram.com/p/ABC/?utm_source=ig_web", "https://www.instagram.com/p/ABC/"),
    # YouTube: every shape reduces to the video id
    ("https://youtu.be/aqz-KE-bpKQ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=42s",
     "https://youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://www.youtube.com/shorts/aqz-KE-bpKQ", "https://youtu.be/aqz-KE-bpKQ"),
    # Threads moved domains
    ("https://www.threads.net/@u/post/ABC", "https://www.threads.com/@u/post/ABC"),
])
def test_urls_that_mean_the_same_post_normalise_equal(a, b):
    assert main.normalize_url(a) == main.normalize_url(b)


@pytest.mark.parametrize("a,b", [
    ("https://www.instagram.com/p/ABC/", "https://www.instagram.com/p/XYZ/"),
    ("https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"),
    ("https://www.instagram.com/p/ABC/", "https://www.threads.com/@u/post/ABC"),
])
def test_different_posts_stay_different(a, b):
    assert main.normalize_url(a) != main.normalize_url(b)


# ── Batch de-duplication ──────────────────────────────────────────────────────

def test_dedupe_keeps_first_occurrence_and_order():
    urls = [
        "https://www.instagram.com/p/AAA/",
        "https://youtu.be/vvvvvvvvvvv",
        "https://www.instagram.com/p/AAA/?img_index=2",   # duplicate of #1
        "https://www.instagram.com/p/BBB/",
        "https://www.youtube.com/watch?v=vvvvvvvvvvv",    # duplicate of #2
    ]
    unique, dupes = main.dedupe_urls(urls)
    assert unique == [
        "https://www.instagram.com/p/AAA/",
        "https://youtu.be/vvvvvvvvvvv",
        "https://www.instagram.com/p/BBB/",
    ]
    assert len(dupes) == 2


def test_dedupe_without_duplicates_is_a_noop():
    urls = ["https://www.instagram.com/p/A/", "https://www.instagram.com/p/B/"]
    unique, dupes = main.dedupe_urls(urls)
    assert unique == urls and dupes == []


def test_dedupe_handles_empty_input():
    assert main.dedupe_urls([]) == ([], [])


# ── Content-level de-duplication ──────────────────────────────────────────────

def test_identical_files_are_detected_and_removed(tmp_path):
    """Two URLs can deliver the same bytes — keep one copy, delete the other."""
    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"
    a.write_bytes(b"same content" * 100)
    b.write_bytes(b"same content" * 100)
    c.write_bytes(b"different" * 100)

    kept, removed = main._dedupe_files([str(a), str(b), str(c)])
    assert kept == [str(a), str(c)]
    assert removed == [str(b)]
    assert not b.exists()          # the duplicate is gone from disk
    assert a.exists() and c.exists()


def test_fingerprint_differs_on_size_alone(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"x" * 1001)
    assert main._file_fingerprint(str(a)) != main._file_fingerprint(str(b))


def test_fingerprint_is_stable(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"stable" * 5000)
    assert main._file_fingerprint(str(p)) == main._file_fingerprint(str(p))


def test_dedupe_files_tolerates_missing_file(tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"data")
    kept, removed = main._dedupe_files([str(real), str(tmp_path / "ghost.bin")])
    assert str(real) in kept and removed == []
