"""MCP tools for podcasts.

These exist because the manual version was the bottleneck: nineteen episodes
looked up one at a time before any analysis could begin. An assistant holding
the links should resolve them in one call.

The tests cover the contract an assistant depends on — what the fields mean,
what happens when nothing is found, and that nothing downloads when the caller
only asked to look.
"""
import asyncio
import inspect

import pytest

import main
import mcp_tools
import podcast

FEED = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <title>Beispielsendung</title><language>de</language>
  <itunes:author>Wer Auch Immer</itunes:author>
  <item>
    <title>Mit Transkript</title><guid>g1</guid>
    <pubDate>Thu, 18 Sep 2026 06:00:00 +0000</pubDate>
    <itunes:duration>10:00</itunes:duration>
    <enclosure url="https://example.com/a.mp3" type="audio/mpeg" length="100"/>
    <podcast:transcript url="https://example.com/a.vtt" type="text/vtt"/>
    <description>Zu Gast ist Anna Beispiel.</description>
  </item>
  <item>
    <title>Ohne Transkript</title><guid>g2</guid>
    <pubDate>Thu, 25 Sep 2026 06:00:00 +0000</pubDate>
    <itunes:duration>20:00</itunes:duration>
    <enclosure url="https://example.com/b.mp3" type="audio/mpeg" length="200"/>
  </item>
</channel></rss>"""

VTT = b"""WEBVTT

00:00:01.000 --> 00:00:04.000
Erster Satz.

00:00:04.000 --> 00:00:07.000
Zweiter Satz.
"""


@pytest.fixture
def offline(monkeypatch):
    """Serve the feed and the transcript from memory.

    The suite stays off the network on purpose, so a show having a bad
    afternoon cannot turn CI red — real requests live in tools/smoke.sh.
    """
    async def fake_fetch(url, timeout=20, as_json=False):
        if url.endswith(".vtt"):
            return VTT
        if "feed" in url or url.endswith(".xml"):
            return FEED
        return None

    async def fake_resolve(item):
        src = item if isinstance(item, str) else item.get("source", "")
        raw = await fake_fetch("feed.xml")
        parsed = podcast.parse_feed(raw)
        want = {"title": "Mit Transkript"} if "a" in src else {"title": "Ohne Transkript"}
        ep, how, note = podcast.match_episode(want, parsed["episodes"])
        return {"status": "ok", "match": how, "note": note, "source": src,
                "feed": "https://x/feed.xml", "show": parsed["show"]["title"],
                "author": parsed["show"]["author"], "language": "de",
                **{k: ep[k] for k in ("guid", "title", "date", "duration_s",
                                      "audio", "shownotes", "transcripts")}}

    monkeypatch.setattr(podcast, "fetch", fake_fetch)
    monkeypatch.setattr(podcast, "resolve", fake_resolve)

    async def fake_many(items, concurrency=4):
        return [await fake_resolve(i) for i in items]
    monkeypatch.setattr(podcast, "resolve_many", fake_many)


def run(coro):
    return asyncio.run(coro)


# ── resolve_podcast ───────────────────────────────────────────────────────────

def test_resolve_returns_a_usable_summary(offline):
    r = run(mcp_tools.resolve_podcast(["https://x/a", "https://x/b"]))
    s = r["summary"]
    assert s["ok"] == 2
    assert s["mit_offiziellem_transkript"] == 1
    # The number that decides whether a Whisper script is needed at all.
    assert s["brauchen_whisper"] == 1


def test_resolve_includes_a_manifest(offline):
    """So an assistant can hand the same structure back to the package tool
    instead of re-resolving."""
    r = run(mcp_tools.resolve_podcast(["https://x/a"]))
    assert r["manifest"]["schema"] == podcast.SCHEMA


def test_resolve_reports_how_each_episode_was_identified(offline):
    r = run(mcp_tools.resolve_podcast(["https://x/a"]))
    assert r["episodes"][0]["match"] in ("guid", "dauer+datum", "titel", "unsicher")


def test_resolve_downloads_nothing():
    """Separating resolution from packaging is the point: an assistant should
    be able to check what it found, and flag uncertain matches, before any
    bytes move."""
    src = inspect.getsource(mcp_tools.resolve_podcast)
    assert "_fetch_to_file" not in src and "_podcast_job" not in src


def test_description_warns_about_uncertain_matches():
    """An assistant that treats 'unsicher' as confirmed will quote the wrong
    episode, and the description is the only place it learns otherwise."""
    tools = run(mcp_tools.mcp.list_tools())
    desc = next(t.description for t in tools if t.name == "resolve_podcast")
    assert "uncertain" in desc
    assert "verify" in desc and "candidate" in desc


# ── get_podcast_shownotes ─────────────────────────────────────────────────────

def test_shownotes_come_back_as_markdown(offline):
    r = run(mcp_tools.get_podcast_shownotes("https://x/a"))
    assert r["markdown"].startswith("---")
    assert "sendung: Beispielsendung" in r["markdown"]


def test_shownotes_say_whether_a_transcript_exists(offline):
    """Saves the caller a second round trip to find out."""
    assert run(mcp_tools.get_podcast_shownotes("https://x/a"))["has_official_transcript"]
    assert not run(mcp_tools.get_podcast_shownotes("https://x/b"))["has_official_transcript"]


def test_shownotes_failure_is_reported_as_data(monkeypatch):
    """An exception out of an MCP tool is opaque to the caller; a field is not."""
    async def not_found(item):
        return {"status": "nicht_aufgeloest", "note": "Feed nicht gefunden."}
    monkeypatch.setattr(podcast, "resolve", not_found)
    r = run(mcp_tools.get_podcast_shownotes("https://x/nope"))
    assert "error" in r and "Feed" in r["error"]


# ── get_podcast_transcript ────────────────────────────────────────────────────

def test_official_transcript_is_fetched_and_parsed(offline):
    r = run(mcp_tools.get_podcast_transcript("https://x/a"))
    assert r["available"] is True
    assert "Erster Satz" in r["text"]
    assert r["segments_count"] == 2


@pytest.mark.parametrize("fmt,needle", [
    # [m:ss], not [mm:ss] — matches what the existing transcript export has
    # always produced, and consistency across the two beats prettiness.
    ("timestamps", "[0:01]"), ("srt", "-->"), ("vtt", "WEBVTT"),
])
def test_formats_are_honoured(offline, fmt, needle):
    r = run(mcp_tools.get_podcast_transcript("https://x/a", fmt))
    assert needle in r["text"]


def test_segments_format_returns_structure(offline):
    r = run(mcp_tools.get_podcast_transcript("https://x/a", "segments"))
    assert isinstance(r["segments"], list) and "start" in r["segments"][0]


def test_missing_transcript_is_not_an_error(offline):
    """A show without a transcript is the common case — three quarters of
    them. It has to read as a normal answer with a next step, not a failure."""
    r = run(mcp_tools.get_podcast_transcript("https://x/b"))
    assert r["available"] is False
    assert "hint" in r and "whisper" in r["hint"].lower()


def test_transcript_source_is_disclosed(offline):
    """Where the text came from decides how much to trust it."""
    r = run(mcp_tools.get_podcast_transcript("https://x/a"))
    assert r["source_url"].endswith(".vtt") and r["source_type"] == "vtt"


def test_server_never_transcribes(offline):
    """Whisper runs on the person's machine. This VPS carries a wiki, a git
    forge and client sites, and has no swap."""
    src = inspect.getsource(mcp_tools.get_podcast_transcript)
    assert "_whisper_segments" not in src and "_transcript_via_whisper" not in src


# ── get_podcast_package ───────────────────────────────────────────────────────

def test_package_requires_a_user():
    """The token is the only thing standing between a caller and someone
    else's files."""
    mcp_tools.current_mcp_user.set(None)
    r = run(mcp_tools.get_podcast_package(["https://x/a"]))
    assert "error" in r


def test_package_validates_the_audio_option():
    mcp_tools.current_mcp_user.set({"id": 1})
    try:
        assert "error" in run(mcp_tools.get_podcast_package(["https://x/a"], "riesig"))
    finally:
        mcp_tools.current_mcp_user.set(None)


def test_package_caps_the_batch():
    mcp_tools.current_mcp_user.set({"id": 1})
    try:
        r = run(mcp_tools.get_podcast_package(["u"] * (main.PODCAST_MAX_EPISODES + 1)))
        assert "error" in r
    finally:
        mcp_tools.current_mcp_user.set(None)


def test_package_returns_urls_and_says_it_is_not_done(monkeypatch):
    """Downloading several episodes outlasts a tool call. Returning a status
    URL plus an explicit note beats a caller assuming the zip is ready."""
    monkeypatch.setattr(db := __import__("db"), "log_event", lambda *a: None)

    async def noop(*a, **kw):
        pass
    monkeypatch.setattr(main, "_podcast_job", noop)
    mcp_tools.current_mcp_user.set({"id": 1})
    try:
        r = run(mcp_tools.get_podcast_package(["https://x/a"]))
        assert r["zip_url"].endswith("/zip") and r["status_url"]
        assert "Hintergrund" in r["note"]
    finally:
        mcp_tools.current_mcp_user.set(None)


# ── get_whisper_script ────────────────────────────────────────────────────────

def test_script_is_built_only_where_needed(offline):
    r = run(mcp_tools.get_whisper_script(["https://x/b"]))
    assert r["script"] and "Ohne" in r["script"]
    assert r["episodes"] == ["Ohne Transkript"]


def test_no_script_when_the_feed_already_has_transcripts(offline):
    r = run(mcp_tools.get_whisper_script(["https://x/a"]))
    assert r["script"] == "" and "offizielles Transkript" in r["note"]


def test_script_names_its_own_file(offline):
    """The caller has to save it under a name the user can act on."""
    assert run(mcp_tools.get_whisper_script(["https://x/b"], "mac"))["filename"].endswith(".command")
    assert run(mcp_tools.get_whisper_script(["https://x/b"], "windows"))["filename"].endswith(".ps1")


def test_unknown_os_is_rejected(offline):
    assert "error" in run(mcp_tools.get_whisper_script(["https://x/b"], "amiga"))


def test_script_note_includes_the_one_time_setup(offline):
    """Without it the user gets 'command not found' and no idea why."""
    r = run(mcp_tools.get_whisper_script(["https://x/b"], "mac"))
    assert "brew install" in r["note"]


# ── Registration ──────────────────────────────────────────────────────────────

def test_all_five_tools_from_the_concept_are_registered():
    names = {t.name for t in run(mcp_tools.mcp.list_tools())}
    assert {"resolve_podcast", "get_podcast_shownotes", "get_podcast_transcript",
            "get_podcast_package", "get_whisper_script"} <= names


def test_descriptions_explain_the_split():
    """An assistant needs to know that resolving is cheap and packaging is
    not, or it will reach for the expensive one first."""
    tools = {t.name: t.description for t in run(mcp_tools.mcp.list_tools())}
    assert "Downloads nothing" in tools["resolve_podcast"]
    assert "background" in tools["get_podcast_package"]


# ── Suche über die Transkripte ───────────────────────────────────────────────
#
# Transkripte zu erzeugen und in einem Ordner liegen zu lassen löst nur die
# halbe Aufgabe. Wer zwanzig Folgen an einem Tag holt, sucht danach nicht die
# Datei, sondern die Stelle.

def _paket(tmp_path):
    a = tmp_path / "2026-10-05_reboot_ki-agenten"; a.mkdir()
    (a / "x_Transkript.vtt").write_text(
        "WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nHeute geht es um Haftung bei KI-Agenten.\n\n"
        "00:23:14.000 --> 00:23:19.000\nDie Produkthaftung gilt ab Dezember auch für Software.\n",
        encoding="utf-8")
    b = tmp_path / "2026-10-02_doppelgaenger_gpus"; b.mkdir()
    (b / "y_Transkript.txt").write_text(
        "Nvidia liefert GPUs in Stückelung.\nÜber Haftung sprechen wir später.\n",
        encoding="utf-8")
    return tmp_path


def test_search_finds_the_place_not_just_the_episode(tmp_path):
    """Der Punkt der Funktion: nicht »in dieser Folge«, sondern »ab 23:14«."""
    hits = main.search_transcripts(_paket(tmp_path), "Produkthaftung")
    assert len(hits) == 1
    assert hits[0]["at"] == "23:14" and hits[0]["seconds"] == 1394


def test_several_words_are_an_and(tmp_path):
    folder = _paket(tmp_path)
    assert len(main.search_transcripts(folder, "produkthaftung software")) == 1
    assert main.search_transcripts(folder, "produkthaftung nvidia") == []


def test_plain_text_gets_no_invented_timestamp(tmp_path):
    """Ohne Zeitmarke keine erfinden. Eine Zeilennummer als Zeit auszugeben
    wäre eine Behauptung, die der Nutzer nicht prüfen kann."""
    hits = [h for h in main.search_transcripts(_paket(tmp_path), "Stückelung")]
    assert len(hits) == 1
    assert hits[0]["at"] == "" and hits[0]["seconds"] is None


def test_search_is_case_insensitive(tmp_path):
    assert main.search_transcripts(_paket(tmp_path), "HAFTUNG")


def test_empty_query_returns_nothing(tmp_path):
    """Sonst wäre eine leere Suche ein vollständiger Dump des Transkripts."""
    folder = _paket(tmp_path)          # einmal aufbauen, nicht pro Durchlauf
    for q in ("", "   ", None):
        assert main.search_transcripts(folder, q) == []


def test_limit_is_honoured(tmp_path):
    folder = _paket(tmp_path)
    assert len(main.search_transcripts(folder, "Haftung", limit=1)) == 1


def test_search_needs_ownership():
    """Die podcast_id ist eine UUID, aber das ist keine Berechtigung."""
    src = inspect.getsource(main.podcast_search)
    assert "_own_podcast" in src
