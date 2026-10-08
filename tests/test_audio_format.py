"""Audio mode: the chosen format reaches yt-dlp and ffmpeg."""
import asyncio

import pytest

import main


class _Stop(Exception):
    pass


def _ytdlp_args(monkeypatch, fmt):
    seen = {}

    async def fake_exec(*args, **kw):
        seen["args"] = list(args)
        raise _Stop

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    main.jobs["j-af"] = {"status": "pending", "audio_fmt": fmt}
    with pytest.raises(_Stop):
        asyncio.run(main._download_single("j-af", "https://example.com/v", "", "best", True))
    main.jobs.pop("j-af", None)
    return seen["args"]


@pytest.mark.parametrize("fmt,expected", [("mp3", "mp3"), ("m4a", "m4a"),
                                          ("opus", "opus"), ("original", "best")])
def test_format_reaches_ytdlp(monkeypatch, fmt, expected):
    args = _ytdlp_args(monkeypatch, fmt)
    i = args.index("--audio-format")
    assert args[i + 1] == expected
    # Quality flag only where it means something; on "best" it would force
    # a re-encode of an otherwise untouched stream.
    assert ("--audio-quality" in args) == (fmt == "mp3")


def test_unknown_format_falls_back_to_mp3():
    main.jobs["j-x"] = {"audio_fmt": "flac; rm -rf /"}
    assert main._audio_fmt("j-x") == "mp3"
    main.jobs.pop("j-x")
    assert main._audio_fmt("missing") == "mp3"


def test_extract_audio_uses_the_codec(monkeypatch, tmp_path):
    calls = []

    async def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return 0, "aac\n", ""
        (tmp_path / cmd[-1].rsplit("/", 1)[-1]).write_bytes(b"x")
        return 0, "", ""

    monkeypatch.setattr(main, "run_cmd", fake_run)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"v")
    out = asyncio.run(main._extract_audio(str(src), "opus"))
    assert out.endswith(".opus") and "libopus" in calls[-1]
    out = asyncio.run(main._extract_audio(str(src), "original"))
    assert out.endswith(".m4a") and calls[-1][calls[-1].index("-c:a") + 1] == "copy"


def test_batch_rejects_an_unknown_format(as_user, alice):
    c = as_user(alice)
    r = c.post("/api/batch", data={"urls": "https://example.com/a", "audio_only": "true",
                                   "audio_format": "flac"})
    assert r.status_code == 400
