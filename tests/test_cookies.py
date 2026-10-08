"""Per-platform cookie stores.

Instagram and YouTube cookies live in separate columns on purpose: YouTube
cookies expire within weeks, Instagram's last far longer, and replacing one
must never wipe the other. The stored values are also never handed back to
the browser — only a summary.
"""
import pytest

import db
import main

IG = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
YT = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\txyz\n"


# ── Storage ───────────────────────────────────────────────────────────────────

def test_both_platforms_are_known():
    assert set(db.COOKIE_COLUMNS) == {"instagram", "youtube"}


def test_columns_are_distinct():
    """One shared column would mean saving YouTube wipes Instagram."""
    assert len(set(db.COOKIE_COLUMNS.values())) == 2


def test_unknown_kind_is_rejected():
    """The column name goes into an f-string SQL statement — allowlist only."""
    with pytest.raises(ValueError):
        db._cookie_column("youtube; DROP TABLE users")


@pytest.mark.parametrize("kind", ["instagram", "youtube"])
def test_column_lookup_round_trip(kind):
    assert db._cookie_column(kind) == db.COOKIE_COLUMNS[kind]


def test_legacy_aliases_point_at_instagram():
    """Older code called set_ig_cookies/get_ig_cookies directly."""
    assert db.set_ig_cookies.__module__ == db.__name__
    assert db.get_ig_cookies.__module__ == db.__name__


# ── Validation ────────────────────────────────────────────────────────────────

def test_json_export_is_rejected():
    """The extension can export JSON too — that silently does nothing."""
    with pytest.raises(main.HTTPException):
        main._validate_cookies('[{"name":"SID","value":"x"}]', "youtube")


def test_swapped_files_are_caught():
    """Pasting the Instagram jar into the YouTube box must not look successful."""
    with pytest.raises(main.HTTPException) as exc:
        main._validate_cookies(IG, "youtube")
    assert "YouTube" in exc.value.detail


def test_swapped_files_are_caught_the_other_way():
    with pytest.raises(main.HTTPException):
        main._validate_cookies(YT, "instagram")


@pytest.mark.parametrize("text,kind", [(IG, "instagram"), (YT, "youtube")])
def test_matching_file_passes(text, kind):
    assert main._validate_cookies(text, kind) == text.strip()


def test_bare_domain_without_leading_dot_is_accepted():
    """Some exports write 'youtube.com', not '.youtube.com'."""
    text = "# Netscape HTTP Cookie File\nyoutube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    assert main._validate_cookies(text, "youtube")


# ── Status is a summary, never the value ──────────────────────────────────────

def test_info_never_leaks_the_cookies():
    info = main._cookie_info(YT, "youtube")
    assert "xyz" not in str(info)
    assert "SID" not in str(info)


def test_info_counts_only_real_lines():
    """Comment lines are not cookies."""
    assert main._cookie_info(YT, "youtube")["lines"] == 1


def test_empty_store_reports_unsaved():
    assert main._cookie_info("", "youtube") == {
        "saved": False, "lines": 0, "matches_platform": False}


def test_info_flags_a_mismatched_platform():
    """Cookies saved before the split may belong to the other platform."""
    assert main._cookie_info(IG, "youtube")["matches_platform"] is False


# ── Merged jar for mixed batches ──────────────────────────────────────────────

def test_merged_jar_contains_both_platforms(monkeypatch):
    monkeypatch.setattr(db, "get_cookies",
                        lambda uid, kind: IG if kind == "instagram" else YT)
    jar = main._merged_cookie_jar(1)
    assert ".instagram.com" in jar and ".youtube.com" in jar


def test_merged_jar_keeps_exactly_one_header(monkeypatch):
    """Two '# Netscape' lines mid-file confuse the parser."""
    monkeypatch.setattr(db, "get_cookies",
                        lambda uid, kind: IG if kind == "instagram" else YT)
    assert main._merged_cookie_jar(1).count("# Netscape") == 1


def test_merged_jar_starts_with_the_header(monkeypatch):
    monkeypatch.setattr(db, "get_cookies",
                        lambda uid, kind: IG if kind == "instagram" else YT)
    assert main._merged_cookie_jar(1).startswith("# Netscape")


def test_single_store_is_passed_through_untouched(monkeypatch):
    monkeypatch.setattr(db, "get_cookies",
                        lambda uid, kind: YT if kind == "youtube" else "")
    assert main._merged_cookie_jar(1) == YT.strip()


def test_no_stores_means_no_jar(monkeypatch):
    monkeypatch.setattr(db, "get_cookies", lambda uid, kind: "")
    assert main._merged_cookie_jar(1) == ""


def test_anonymous_user_has_no_jar():
    assert main._merged_cookie_jar(None) == ""
