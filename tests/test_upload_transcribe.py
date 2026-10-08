"""Transcribing files people bring themselves, and getting script results back
into a podcast package.

Both routes take a file from the browser and write to disk, so the tests
stay with what can go wrong there: oversized and wrong-typed uploads, a
Whisper that is switched off, a slug that tries to leave the package, and
the one outcome that makes the feature worth having: the text is afterwards
exportable or searchable through the routes that already existed.
"""
import asyncio
import uuid

import pytest

import main
import podcast


SEGS = [{"start": 0.0, "dur": 2.0, "text": "Hallo von Gregor Schmalzried."},
        {"start": 2.0, "dur": 2.0, "text": "Wir reden über Kubernetes."}]


@pytest.fixture
def whisper(monkeypatch, tmp_path):
    """Whisper on, ffprobe and the model replaced. Records what the model
    was asked so the glossary can be checked end to end."""
    calls = []

    async def fake_segments(path, lang, prompt="", timeout=0):
        calls.append({"path": path, "exists": path.exists(), "lang": lang, "prompt": prompt})
        return list(SEGS), "de"

    async def fake_duration(path):
        return 120.0

    async def fake_normalise(src):
        out = src.with_name(src.stem + "_norm.wav")
        out.write_bytes(src.read_bytes())
        return out

    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "TMP_DIR", tmp_path)
    monkeypatch.setattr(main, "_whisper_segments", fake_segments)
    monkeypatch.setattr(main, "_media_duration", fake_duration)
    monkeypatch.setattr(main, "_normalise_upload", fake_normalise)
    return calls


def _upload(c, name="interview.m4a", body=b"\0" * 4096, **form):
    data = {"lang": "de", **form}
    return c.post("/api/transcript/upload", files={"file": (name, body)}, data=data)


# ── Own file ──────────────────────────────────────────────────────────────────

def test_happy_path_produces_an_exportable_transcript(as_user, alice, whisper, tmp_path):
    c = as_user(alice)
    r = _upload(c)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    job = c.get(f"/api/job/{job_id}").json()
    assert job["status"] == "done"
    assert job["type"] == "transcript"
    assert "Kubernetes" in job["transcript"]
    assert job["meta"]["title"] == "interview"

    srt = c.get(f"/api/transcript/{job_id}/export.srt")
    assert srt.status_code == 200
    assert "Gregor Schmalzried" in srt.text and "-->" in srt.text

    # Whisper saw the file, and nothing is left behind afterwards.
    assert whisper and whisper[0]["exists"] is True
    assert list(tmp_path.iterdir()) == []


def test_glossary_reaches_whisper_capped(as_user, alice, whisper):
    c = as_user(alice)
    _upload(c, prompt="Gregor Schmalzried\nKubernetes")
    assert whisper[-1]["prompt"] == "Gregor Schmalzried, Kubernetes"

    _upload(c, prompt="x" * 500)
    assert len(whisper[-1]["prompt"]) == 200


def test_glossary_is_passed_on_the_url_path_too(as_user, alice, monkeypatch):
    """The URL transcript forwards it to the Whisper fallback."""
    seen = {}

    async def fake_job(job_id, url, lang, user_id=None, prompt=""):
        seen["prompt"] = prompt

    monkeypatch.setattr(main, "_transcript_job", fake_job)
    r = as_user(alice).post("/api/transcript", data={
        "url": "https://youtu.be/abc123", "lang": "de", "prompt": "Fundus,  Whisper"})
    assert r.status_code == 200
    assert seen["prompt"] == "Fundus, Whisper"


def test_url_whisper_fallback_uses_the_prompt(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"0")
    got = {}

    async def fake_audio(*a, **kw):
        return audio

    async def fake_duration(*a, **kw):
        return 60.0

    async def fake_segments(path, lang, prompt=""):
        got["prompt"] = prompt
        return list(SEGS), "de"

    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "_download_audio", fake_audio)
    monkeypatch.setattr(main, "_media_duration", fake_duration)
    monkeypatch.setattr(main, "_whisper_segments", fake_segments)
    segs, _ = asyncio.run(main._transcript_via_whisper(
        "j", "https://youtu.be/x", "de", prompt="Gregor Schmalzried"))
    assert segs and got["prompt"] == "Gregor Schmalzried"


def test_wrong_extension_is_refused(as_user, alice, whisper, tmp_path):
    r = _upload(as_user(alice), name="payload.exe")
    assert r.status_code == 400
    assert list(tmp_path.iterdir()) == []
    assert whisper == []


def test_declared_oversize_is_refused_before_parsing(as_user, alice, whisper, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOAD_MAX_MB", 1)
    r = _upload(as_user(alice), body=b"\0" * (3 * 1024 * 1024))
    assert r.status_code == 413
    assert whisper == []


def test_oversize_within_header_slack_is_caught_while_streaming(as_user, alice, whisper,
                                                                 monkeypatch, tmp_path):
    """The header check leaves room for multipart overhead; the byte count in
    the handler is the guard that actually holds, and it cleans up."""
    monkeypatch.setattr(main, "UPLOAD_MAX_MB", 1)
    r = _upload(as_user(alice), body=b"\0" * (1024 * 1024 + 300 * 1024))
    assert r.status_code == 413
    assert list(tmp_path.iterdir()) == []
    assert whisper == []


def test_too_long_recording_is_refused(as_user, alice, whisper, monkeypatch, tmp_path):
    async def long(path):
        return (main.WHISPER_MAX_MINUTES + 5) * 60.0
    monkeypatch.setattr(main, "_media_duration", long)
    r = _upload(as_user(alice))
    assert r.status_code == 413
    assert "Minuten" in r.json()["detail"]
    assert list(tmp_path.iterdir()) == []


def test_disabled_whisper_says_so(as_user, alice, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "WHISPER_ENABLED", False)
    monkeypatch.setattr(main, "TMP_DIR", tmp_path)
    r = _upload(as_user(alice))
    assert r.status_code == 503
    assert "WHISPER_ENABLED" in r.json()["detail"]


def test_other_users_cannot_read_the_job(as_user, alice, bob, whisper):
    job_id = _upload(as_user(alice)).json()["job_id"]
    c = as_user(bob)
    assert c.get(f"/api/job/{job_id}").status_code == 404
    assert c.get(f"/api/transcript/{job_id}/export.txt").status_code == 404


# ── Podcast: server-side Whisper is opt-in ────────────────────────────────────

EP = {"status": "ok", "show": "Sendung", "title": "Folge", "date": "",
      "duration_s": 600, "language": "de", "transcripts": []}


@pytest.fixture
def podcast_whisper(monkeypatch):
    async def fake(path, lang, prompt=""):
        return list(SEGS), "de"
    monkeypatch.setattr(main, "_whisper_segments", fake)
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "LOCAL_MODE", False)


def test_server_stays_out_of_podcasts_by_default(podcast_whisper, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PODCAST_SERVER_WHISPER", False)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    assert asyncio.run(main._podcast_transcribe_local(
        {}, dict(EP), tmp_path, "x", ["x.mp3"])) is False


def test_server_opt_in_transcribes(podcast_whisper, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PODCAST_SERVER_WHISPER", True)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    ep, files = dict(EP), ["x.mp3"]
    assert asyncio.run(main._podcast_transcribe_local({}, ep, tmp_path, "x", files)) is True
    assert "x_Transkript.vtt" in files
    assert ep["transcript_source"].startswith("whisper-server")


def test_server_opt_in_still_honours_the_length_ceiling(podcast_whisper, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PODCAST_SERVER_WHISPER", True)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    ep = dict(EP, duration_s=(main.WHISPER_MAX_MINUTES + 1) * 60)
    assert asyncio.run(main._podcast_transcribe_local({}, ep, tmp_path, "x", ["x.mp3"])) is False


def test_server_opt_in_still_needs_whisper_enabled(podcast_whisper, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PODCAST_SERVER_WHISPER", True)
    monkeypatch.setattr(main, "WHISPER_ENABLED", False)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    assert asyncio.run(main._podcast_transcribe_local(
        {}, dict(EP), tmp_path, "x", ["x.mp3"])) is False


def test_concurrency_limit_comes_from_the_environment():
    assert isinstance(main._whisper_lock, asyncio.Semaphore)
    assert main._whisper_lock._value == main.WHISPER_CONCURRENCY


# ── Podcast: script results back into the package ─────────────────────────────

VTT = ("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHeute geht es um Zettelkasten.\n\n"
       "00:01:05.000 --> 00:01:09.000\nUnd danach um Obsidian.\n")
SRT = ("1\n00:00:01,000 --> 00:00:04,000\nHeute geht es um Zettelkasten.\n\n"
       "2\n00:01:05,000 --> 00:01:09,000\nUnd danach um Obsidian.\n")


@pytest.fixture
def package(alice):
    pid = str(uuid.uuid4())
    ep = dict(EP, title="Wissensarbeit")
    slug = podcast.episode_slug(ep)
    main._podcast_dir(pid).joinpath(slug).mkdir(parents=True)
    main.podcasts[pid] = {"status": "done", "user_id": alice["id"], "episodes": [ep]}
    yield pid, slug
    main.podcasts.pop(pid, None)


def test_uploaded_vtt_becomes_searchable(as_user, alice, package):
    pid, slug = package
    c = as_user(alice)
    assert c.get(f"/api/podcast/{pid}/search", params={"q": "obsidian"}).json()["count"] == 0

    r = c.post(f"/api/podcast/{pid}/transcript/{slug}",
               files={"file": (f"{slug}.vtt", VTT.encode())})
    assert r.status_code == 200, r.text

    hits = c.get(f"/api/podcast/{pid}/search", params={"q": "obsidian"}).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["at"] == "1:05" and hits[0]["title"] == "Wissensarbeit"


def test_uploaded_srt_keeps_its_time_marks(as_user, alice, package):
    pid, slug = package
    c = as_user(alice)
    r = c.post(f"/api/podcast/{pid}/transcript/{slug}",
               files={"file": (f"{slug}.srt", SRT.encode())})
    assert r.status_code == 200, r.text
    hits = c.get(f"/api/podcast/{pid}/search", params={"q": "zettelkasten"}).json()["hits"]
    assert hits and hits[0]["seconds"] == 1


def test_status_tells_the_browser_each_slug(as_user, alice, package):
    pid, slug = package
    eps = as_user(alice).get(f"/api/podcast/{pid}").json()["episodes"]
    assert eps[0]["slug"] == slug


@pytest.mark.parametrize("bad", ["..", "...", "x", "manifest.json"])
def test_unknown_or_traversing_slug_is_refused(as_user, alice, package, bad):
    pid, _ = package
    r = as_user(alice).post(f"/api/podcast/{pid}/transcript/{bad}",
                            files={"file": ("a.vtt", VTT.encode())})
    # ".." is collapsed by the client into a different path (405); the rest
    # reach the handler and miss the known set (404). Either way: refused.
    assert r.status_code in (404, 405)
    # Nothing was written outside the episode folder.
    folder = main._podcast_dir(pid)
    assert not any(p.is_file() for p in folder.parent.glob("*_Transkript.*"))
    assert not any(p.is_file() for p in folder.glob("*_Transkript.*"))


def test_encoded_traversal_does_not_reach_the_handler(as_user, alice, package):
    pid, _ = package
    r = as_user(alice).post(f"/api/podcast/{pid}/transcript/..%2F..%2Fescape",
                            files={"file": ("a.vtt", VTT.encode())})
    assert r.status_code in (404, 405)
    assert not (main.DOWNLOAD_DIR.parent / "escape_Transkript.vtt").exists()


def test_package_upload_is_owner_only(as_user, bob, package):
    pid, slug = package
    r = as_user(bob).post(f"/api/podcast/{pid}/transcript/{slug}",
                          files={"file": (f"{slug}.vtt", VTT.encode())})
    assert r.status_code == 404


def test_package_upload_type_and_size(as_user, alice, package):
    pid, slug = package
    c = as_user(alice)
    r = c.post(f"/api/podcast/{pid}/transcript/{slug}",
               files={"file": (f"{slug}.json", b"{}")})
    assert r.status_code == 400
    big = b"a" * (main.PODCAST_TRANSCRIPT_MAX_BYTES + 10)
    r = c.post(f"/api/podcast/{pid}/transcript/{slug}", files={"file": (f"{slug}.txt", big)})
    assert r.status_code == 413


# ── Hardening after review ────────────────────────────────────────────────────

def test_upload_without_content_length_is_refused(as_user, alice, whisper):
    """Chunked bodies have no Content-Length and would be spooled in full
    before any size check could run."""
    c = as_user(alice)
    r = c.post("/api/transcript/upload", content=b"x" * 10,
               headers={"transfer-encoding": "chunked",
                        "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411


def test_at_most_two_open_uploads_per_person(as_user, alice, whisper, monkeypatch):
    for i in range(2):
        main.jobs[f"open-{i}"] = {"upload": True, "user_id": alice["id"], "status": "running"}
    try:
        assert _upload(as_user(alice)).status_code == 429
    finally:
        for i in range(2):
            main.jobs.pop(f"open-{i}", None)


def test_ffmpeg_reads_uploads_with_local_files_only():
    """A '.mp3' that is really a playlist must not make ffmpeg open URLs."""
    assert main._UPLOAD_FF_GUARD[:2] == ["-protocol_whitelist", "file"]
    assert "-format_whitelist" in main._UPLOAD_FF_GUARD
    assert "hls" not in main._UPLOAD_FF_GUARD[3] and "concat" not in main._UPLOAD_FF_GUARD[3]


def test_whisper_slot_stays_taken_until_the_thread_ends(monkeypatch, tmp_path):
    """A timed-out run keeps computing; the next one must not start beside it."""
    import asyncio, threading, time
    release = threading.Event()
    monkeypatch.setattr(main, "_mlx_available", lambda: False)

    class Model:
        def transcribe(self, *a, **k):
            release.wait(5)
            return [], type("I", (), {"language": "de"})()
    monkeypatch.setattr(main, "_load_whisper", lambda: Model())
    monkeypatch.setattr(main, "_whisper_lock", asyncio.Semaphore(1))

    async def scenario():
        main._whisper_lock = asyncio.Semaphore(1)
        try:
            await main._whisper_segments(tmp_path / "a.wav", "de", timeout=0.2)
        except asyncio.TimeoutError:
            pass
        held = main._whisper_lock.locked()
        release.set()
        await asyncio.sleep(0.3)
        return held, main._whisper_lock.locked()
    held, after = asyncio.run(scenario())
    assert held is True and after is False
