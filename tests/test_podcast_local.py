"""Podcast transcription on the desktop.

On the server this is excluded outright — no swap, and a wiki, a git forge and
client sites share the box. On a laptop the premise inverts: the machine that
would run the handed-over script *is* this machine, so handing over a script
is friction for its own sake.

The tests pin the boundary (server never transcribes), and they pin the thing
that is easy to get wrong once both paths exist: a script that redoes work
already finished would burn an hour for nothing.
"""
import asyncio
import inspect
from pathlib import Path

import pytest

import main


def run(coro):
    return asyncio.run(coro)


EP = {"status": "ok", "show": "Sendung", "title": "Folge", "date": "",
      "duration_s": 600, "language": "de", "transcripts": []}


# ── The boundary ──────────────────────────────────────────────────────────────

def test_server_refuses_to_transcribe(monkeypatch, tmp_path):
    """The one rule from the concept that is not negotiable."""
    monkeypatch.setattr(main, "LOCAL_MODE", False)
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    assert run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", ["x.mp3"])) is False


def test_disabled_whisper_is_respected(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    monkeypatch.setattr(main, "WHISPER_ENABLED", False)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    assert run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", ["x.mp3"])) is False


def test_missing_audio_is_not_an_error(monkeypatch, tmp_path):
    """Audio is optional — someone may have asked for notes only."""
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    assert run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", [])) is False


# ── The desktop path ──────────────────────────────────────────────────────────

@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "WHISPER_MODEL", "large-v3-turbo")

    async def fake(path, lang):
        return [{"start": 1.0, "dur": 3.0, "text": "Erster Satz."},
                {"start": 4.0, "dur": 3.0, "text": "Zweiter Satz."}], "de"
    monkeypatch.setattr(main, "_whisper_segments", fake)


def test_all_four_formats_are_written(local, tmp_path):
    """Same set the feed-supplied transcripts get, so a package reads the same
    regardless of where its text came from."""
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    files, ep = ["x.mp3"], dict(EP)
    assert run(main._podcast_transcribe_local({}, ep, tmp_path, "x", files)) is True
    for suffix in ("txt", "zeitmarken.txt", "srt", "vtt"):
        assert (tmp_path / f"x_Transkript.{suffix}").exists()
        assert f"x_Transkript.{suffix}" in files


def test_timestamped_version_exists(local, tmp_path):
    """The form you quote from. Everything else is for reading."""
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", ["x.mp3"]))
    body = (tmp_path / "x_Transkript.zeitmarken.txt").read_text()
    assert "[0:01]" in body and "Erster Satz" in body


def test_provenance_is_recorded(local, tmp_path):
    """Whisper mishears proper nouns. Anyone quoting from this needs to know
    which kind of transcript they are holding."""
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    ep = dict(EP)
    run(main._podcast_transcribe_local({}, ep, tmp_path, "x", ["x.mp3"]))
    assert "whisper" in ep["transcript_source"]
    assert "large-v3-turbo" in ep["transcript_source"]


def test_m4a_is_handled(local, tmp_path):
    """One of the eight shows ships m4a."""
    (tmp_path / "x.m4a").write_bytes(b"0" * 2048)
    assert run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", ["x.m4a"])) is True


def test_progress_is_reported(local, tmp_path):
    """A ten-minute episode takes a while even on the Neural Engine; silence
    reads as a hang."""
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    rec = {}
    run(main._podcast_transcribe_local(rec, dict(EP), tmp_path, "x", ["x.mp3"]))
    assert "stage" in rec and "Min" in rec["stage"]


def test_whisper_failure_does_not_break_the_package(monkeypatch, tmp_path):
    """Shownotes and audio are already on disk and still worth having."""
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)

    async def boom(path, lang):
        raise RuntimeError("Modell nicht geladen")
    monkeypatch.setattr(main, "_whisper_segments", boom)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    assert run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", ["x.mp3"])) is False


def test_empty_result_is_treated_as_failure(monkeypatch, tmp_path):
    """An empty transcript file is worse than none: it looks like the episode
    had nothing to say."""
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)

    async def nothing(path, lang):
        return [], ""
    monkeypatch.setattr(main, "_whisper_segments", nothing)
    (tmp_path / "x.mp3").write_bytes(b"0" * 2048)
    assert run(main._podcast_transcribe_local({}, dict(EP), tmp_path, "x", ["x.mp3"])) is False
    assert not list(tmp_path.glob("*_Transkript.*"))


# ── Not doing the work twice ──────────────────────────────────────────────────

def test_script_skips_locally_finished_episodes():
    """The mistake that becomes possible the moment both paths exist: a
    script that transcribes an episode that is already done."""
    eps = [{"status": "ok", "show": "A", "title": "Erledigt", "transcripts": [],
            "transcript_source": "whisper-lokal (large-v3-turbo)"},
           {"status": "ok", "show": "B", "title": "Offen", "transcripts": []}]
    s = main.build_podcast_whisper_script(eps, "mac")
    assert "Offen" in s and "Erledigt" not in s


def test_no_script_when_everything_is_handled():
    eps = [{"status": "ok", "show": "A", "title": "T", "transcripts": [],
            "transcript_source": "whisper-lokal (base)"}]
    assert main.build_podcast_whisper_script(eps, "mac") == ""


def test_readme_counts_local_transcripts_separately():
    """Three sources, three numbers: feed, this machine, still open. Lumping
    them together hides which texts to trust."""
    src = inspect.getsource(main._podcast_job)
    assert "done_locally" in src
    assert "transcript_source" in src


def test_transcription_runs_after_the_download():
    """It needs the audio file, so order is not cosmetic."""
    src = inspect.getsource(main._podcast_job)
    assert src.index("_fetch_to_file") < src.index("_podcast_transcribe_local")


def test_only_episodes_without_a_feed_transcript_are_transcribed():
    """Re-doing what the publisher already provides wastes an hour and gives
    a worse result."""
    src = inspect.getsource(main._podcast_job)
    i = src.index("_podcast_transcribe_local")
    assert 'if not ep.get("transcripts")' in src[max(0, i - 400):i]


# ── Bundling (the build that shipped without podcast.py) ─────────────────────

MACOS = Path(__file__).resolve().parent.parent / "macos"


def test_build_copies_every_python_module():
    """A hand-kept list shipped an app without podcast.py, and it surfaced as
    an ImportError at launch — long after the build reported success."""
    src = (MACOS / "build-app.sh").read_text()
    assert '"$SERVER_SRC"/*.py' in src


def test_xcode_project_uses_a_glob_too():
    assert '"$SRC"/*.py' in (MACOS / "project.yml").read_text()


def test_build_verifies_its_own_imports():
    """The guard against the next variant of the same mistake: fail at build
    time, not at launch."""
    src = (MACOS / "build-app.sh").read_text()
    assert "nicht eingebettet" in src
    assert "ast.walk" in src
