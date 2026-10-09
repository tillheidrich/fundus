"""Regressions found by testing the public claims end to end (10/2026).

Each test pins one behaviour the README or a launch post promises, at the
spot where a real run showed it did not hold.
"""
import asyncio

import pytest

import main
import mcp_tools

SEGS = [{"start": 0.0, "dur": 2.0, "text": "Hello there."}]


def run(coro):
    return asyncio.run(coro)


# ── Region language tags ──────────────────────────────────────────────────────
# A public feed said <language>en-gb</language>; Whisper refused the file and
# the package silently came without a transcript.

@pytest.mark.parametrize("tag,want", [("en-gb", "en"), ("en-US", "en"), ("de-DE", "de"),
                                      ("de", "de"), ("EN", "en")])
def test_safe_lang_keeps_only_the_primary_subtag(tag, want):
    assert main.safe_lang(tag) == want


def test_safe_lang_still_rejects_garbage():
    assert main.safe_lang("de; curl x|sh", "de") == "de"


def test_whisper_gets_a_plain_language_code(monkeypatch, tmp_path):
    seen = {}

    class FakeModel:
        def transcribe(self, path, **kw):
            seen["language"] = kw.get("language")

            class Info:
                language = "en"
            return iter(()), Info()

    monkeypatch.setattr(main, "_mlx_available", lambda: False)
    monkeypatch.setattr(main, "_load_whisper", lambda: FakeModel())
    f = tmp_path / "a.wav"
    f.write_bytes(b"0")
    run(main._whisper_segments(f, "en-gb"))
    assert seen["language"] == "en"


def test_generated_script_uses_plain_language_code():
    ep = {"status": "ok", "show": "S", "title": "T", "date": "2026-10-01",
          "language": "en-us", "duration_s": 60}
    for os_kind in ("mac", "windows"):
        script = main.build_podcast_whisper_script([ep], os_kind)
        assert "--language en-us" not in script and "--language en " in script


# ── YouTube audio follows the YouTube switch ─────────────────────────────────

def _fallback_fakes(monkeypatch, tmp_path, fetched):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"0")

    async def fake_audio(job_id, url, cookies=""):
        fetched.append(url)
        return audio

    async def fake_duration(*a, **kw):
        return 30.0

    async def fake_segments(path, lang, prompt="", timeout=0):
        return list(SEGS), "en"

    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "_extractor_installed", lambda: True)
    monkeypatch.setattr(main, "_download_audio", fake_audio)
    monkeypatch.setattr(main, "_media_duration", fake_duration)
    monkeypatch.setattr(main, "_whisper_segments", fake_segments)


def test_youtube_audio_is_not_fetched_while_youtube_is_off(monkeypatch, tmp_path):
    fetched = []
    _fallback_fakes(monkeypatch, tmp_path, fetched)
    monkeypatch.setattr(main, "youtube_video_enabled", lambda: False)
    segs, _ = run(main._transcript_via_whisper("j", "https://youtu.be/abcdefghijk", "en"))
    assert segs == [] and fetched == []


def test_youtube_audio_is_fetched_when_switched_on(monkeypatch, tmp_path):
    fetched = []
    _fallback_fakes(monkeypatch, tmp_path, fetched)
    monkeypatch.setattr(main, "youtube_video_enabled", lambda: True)
    segs, _ = run(main._transcript_via_whisper("j", "https://youtu.be/abcdefghijk", "en"))
    assert segs and fetched


def test_no_extractor_no_audio_fallback(monkeypatch, tmp_path):
    fetched = []
    _fallback_fakes(monkeypatch, tmp_path, fetched)
    monkeypatch.setattr(main, "_extractor_installed", lambda: False)
    monkeypatch.setattr(main, "youtube_video_enabled", lambda: True)
    segs, _ = run(main._transcript_via_whisper("j", "https://example.com/v.mp4", "en"))
    assert segs == [] and fetched == []


# ── Short videos without subtitles get transcribed ───────────────────────────

def test_reel_without_subtitles_falls_back_to_whisper(monkeypatch, tmp_path):
    fetched = []
    _fallback_fakes(monkeypatch, tmp_path, fetched)

    async def meta(url, cookies=""):
        return {"title": "Reel", "webpage_url": url}

    async def subs(*a, **kw):
        return 0, ""

    async def caption(url):
        return ""

    monkeypatch.setattr(main, "_fetch_light_meta", meta)
    monkeypatch.setattr(main, "_fetch_subs", subs)
    monkeypatch.setattr(main, "_pick_sub_file", lambda *a: None)
    monkeypatch.setattr(main, "_fetch_caption", caption)
    jid = "reeltest"
    main.jobs[jid] = {"status": "pending", "url": "x", "type": "transcript"}
    try:
        run(main._transcript_job_inner(jid, "https://www.instagram.com/reel/ABC/", "native"))
        job = main.jobs[jid]
    finally:
        main.jobs.pop(jid, None)
    assert job["status"] == "done" and job["meta"].get("via_whisper")
    assert fetched == ["https://www.instagram.com/reel/ABC/"]


# ── Without yt-dlp: clear answers, no bare errno ─────────────────────────────

def test_run_ytdlp_without_extractor_says_so(monkeypatch):
    monkeypatch.setattr(main, "_ytdlp_cmd", lambda: ["/nonexistent/yt-dlp-binary"])
    code, out, err = run(main.run_ytdlp(["--version"]))
    assert code == 127 and "yt-dlp" in err


def test_mcp_get_transcript_uses_the_web_pipeline(monkeypatch):
    """The stock image has no yt-dlp. YouTube captions come through the
    timedtext API there, and the MCP tool must take that path too."""
    calls = []

    async def fake_job(job_id, url, lang, user_id=None, prompt=""):
        calls.append(url)
        main._finish_transcript(job_id, list(SEGS), "en",
                                {"title": "Me at the zoo", "webpage_url": url}, True)

    monkeypatch.setattr(main, "_transcript_job", fake_job)
    monkeypatch.setattr(mcp_tools, "_uid", lambda: None)
    monkeypatch.setattr(mcp_tools, "_throttled", lambda n: None)
    out = run(mcp_tools.get_transcript("https://www.youtube.com/watch?v=jNQXAC9IVRw"))
    assert out["ok"] and out["transcript"] == "Hello there." and out["lang"] == "en"
    assert calls and not any(k.startswith("mcp_") for k in main.jobs)


def test_mcp_get_caption_without_extractor(monkeypatch):
    monkeypatch.setattr(main, "_extractor_installed", lambda: False)
    out = run(mcp_tools.get_caption("https://www.instagram.com/p/x/"))
    assert out["ok"] is False and "yt-dlp" in out["error"]


# ── Export formats ────────────────────────────────────────────────────────────

def test_unknown_export_format_is_refused(as_user, alice):
    c = as_user(alice)
    jid = "exp_claims"
    main.jobs[jid] = {"status": "pending", "user_id": alice["id"], "url": "u"}
    main._finish_transcript(jid, list(SEGS), "en", {"title": "T"}, True)
    try:
        assert c.get(f"/api/transcript/{jid}/export.pdf").status_code == 400
        for fmt in ("txt", "md", "srt", "vtt", "json"):
            assert c.get(f"/api/transcript/{jid}/export.{fmt}").status_code == 200
    finally:
        main.jobs.pop(jid, None)


def test_html_lang_follows_the_interface_language(as_user, alice):
    c = as_user(alice)
    c.cookies.set("hd_lang", "en")
    assert '<html lang="en">' in c.get("/").text
    c.cookies.set("hd_lang", "de")
    assert '<html lang="de">' in c.get("/").text


# ── Off switch for the version check ──────────────────────────────────────────

def test_check_interval_zero_silences_the_boot_check(monkeypatch):
    called = []

    async def fake_check():
        called.append(1)
        return {}

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(main, "_check_ytdlp_latest", fake_check)
    monkeypatch.setattr(main, "CHECK_INTERVAL_H", 0)
    monkeypatch.setattr(main, "UPDATE_INTERVAL_H", 0)

    async def go():
        await main._boot()
        await asyncio.sleep(0.05)
    run(go())
    assert called == []

    monkeypatch.setattr(main, "CHECK_INTERVAL_H", 24)
    run(go())
    assert called == [1]


def test_unknown_language_is_left_to_detection(monkeypatch, tmp_path):
    seen = {}

    class FakeModel:
        def transcribe(self, path, **kw):
            seen["language"] = kw.get("language")

            class Info:
                language = "en"
            return iter(()), Info()

    monkeypatch.setattr(main, "_mlx_available", lambda: False)
    monkeypatch.setattr(main, "_load_whisper", lambda: FakeModel())
    monkeypatch.setattr(main, "_whisper_knows", lambda c: c in ("en", "de"))
    f = tmp_path / "a.wav"
    f.write_bytes(b"0")
    run(main._whisper_segments(f, "na"))
    assert seen["language"] is None


def test_ytdlp_na_placeholders_are_dropped(monkeypatch):
    async def fake_run(args):
        return 0, "Title\nNA\nNA\nhttps://x\nNA\nNA\n", ""
    monkeypatch.setattr(main, "run_ytdlp", fake_run)
    m = run(main._fetch_light_meta("https://archive.org/details/x"))
    assert m["language"] == "" and m["uploader"] == "" and m["title"] == "Title"
